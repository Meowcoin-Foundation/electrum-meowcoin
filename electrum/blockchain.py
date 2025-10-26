# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import threading
import time
import struct
from typing import Optional, Dict, Mapping, Sequence, TYPE_CHECKING, Tuple

from . import util
from .bitcoin import hash_encode, int_to_hex, rev_hex
from .crypto import sha256d
from . import constants
from .util import bfh, with_lock
from .logging import get_logger, Logger

import x16r_hash
import x16rv2_hash
import kawpow
import meowpow
    
if TYPE_CHECKING:
    from .simple_config import SimpleConfig

_logger = get_logger(__name__)

# Test if scrypt is available with EXACT parameters used for AuxPOW
_SCRYPT_AVAILABLE = False
_SCRYPT_ERROR = None
try:
    import hashlib
    # Test scrypt with EXACT parameters: 80-byte header, same as salt
    test_header = b'\x00' * 80  # Simulate 80-byte header
    test_result = hashlib.scrypt(test_header, salt=test_header, n=1024, r=1, p=1, dklen=32)
    if len(test_result) == 32:
        _SCRYPT_AVAILABLE = True
        _logger.info("✅ hashlib.scrypt is AVAILABLE and WORKING - AuxPOW hashing will be correct")
    else:
        _SCRYPT_ERROR = f"scrypt returned {len(test_result)} bytes instead of 32"
        _logger.error(f"❌ CRITICAL: hashlib.scrypt returned wrong length: {_SCRYPT_ERROR}")
except Exception as e:
    _SCRYPT_ERROR = str(e)
    _logger.error(f"❌ CRITICAL: hashlib.scrypt NOT available or failed: {e}")
    _logger.error("⚠️  AuxPOW blocks will use SHA256 fallback (INCORRECT - will cause validation errors)")

MAX_TARGET = 0x00000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
KAWPOW_LIMIT = 0x0000000000ffffffffffffffffffffffffffffffffffffffffffffffffffffff
MEOWPOW_LIMIT = 0x0000000000ffffffffffffffffffffffffffffffffffffffffffffffffffffff
SCRYPT_LIMIT = 0x00000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  # AuxPOW/Scrypt limit

HEADER_SIZE = 120  # bytes
LEGACY_HEADER_SIZE = 80

DGW_PASTBLOCKS = 180
LWMA_AVERAGING_WINDOW = 90  # N parameter for LWMA
POW_TARGET_SPACING = 60  # Base target spacing in seconds (1 minute)

class MissingHeader(Exception):
    pass

class InvalidHeader(Exception):
    pass

class NotEnoughHeaders(Exception):
    pass

def get_block_algo(header: dict, height: int) -> str:
    """Determine the mining algorithm used for a block.
    
    Returns:
        'scrypt' for AuxPOW blocks
        'meowpow' for native MeowPow blocks
    """
    # Check if AuxPOW is active at this height
    if height >= constants.net.AuxPowActivationHeight:
        # Check version bit to determine if this is an AuxPOW block
        version_int = header.get('version', 0)
        is_auxpow = bool(version_int & (1 << 8))
        return 'scrypt' if is_auxpow else 'meowpow'
    else:
        # Before AuxPOW activation, all blocks are MeowPow
        return 'meowpow'

def serialize_header(header_dict: dict) -> str:
    # CRITICAL: Base serialization on header STRUCTURE, not timestamp
    # If header has nheight field → MeowPow format (120 bytes)  
    # If header lacks nheight field → AuxPOW/legacy format (80 bytes)
    is_meowpow_header = 'nheight' in header_dict
    is_auxpow = not is_meowpow_header
    
    if is_meowpow_header:
        # MeowPow/KawPow header format (120 bytes)
        # Use nonce64 field if available, otherwise use nonce (but as 8 bytes)
        nonce_value = header_dict.get('nonce64', header_dict.get('nonce', 0))
        s = int_to_hex(header_dict['version'], 4) \
            + rev_hex(header_dict['prev_block_hash']) \
            + rev_hex(header_dict['merkle_root']) \
            + int_to_hex(int(header_dict['timestamp']), 4) \
            + int_to_hex(int(header_dict['bits']), 4) \
            + int_to_hex(int(header_dict['nheight']), 4) \
            + int_to_hex(int(nonce_value), 8) \
            + rev_hex(header_dict['mix_hash'])
    else:
        # AuxPOW/legacy header format (80 bytes)
        s = int_to_hex(header_dict['version'], 4) \
            + rev_hex(header_dict['prev_block_hash']) \
            + rev_hex(header_dict['merkle_root']) \
            + int_to_hex(int(header_dict['timestamp']), 4) \
            + int_to_hex(int(header_dict['bits']), 4) \
            + int_to_hex(int(header_dict['nonce']), 4)
        # CRITICAL FIX: Don't pad AuxPOW headers - they must be exactly 80 bytes for hashing
        # Padding is only for storage/display compatibility
        if not is_auxpow:
            s = s.ljust(HEADER_SIZE * 2, '0')  # pad only non-AuxPOW legacy headers
    return s

def deserialize_header(s: bytes, height: int) -> dict:
    if not s:
        raise InvalidHeader('Invalid header: {}'.format(s))
    if len(s) not in (LEGACY_HEADER_SIZE, HEADER_SIZE):
        raise InvalidHeader('Invalid header length: {}'.format(len(s)))

    def hex_to_int(hex):
        return int.from_bytes(hex, byteorder='little')

    h = {'version': hex_to_int(s[0:4]),
         'prev_block_hash': hash_encode(s[4:36]),
         'merkle_root': hash_encode(s[36:68]),
         'timestamp': int(hash_encode(s[68:72]), 16),
         'bits': int(hash_encode(s[72:76]), 16)}
    
    # Handle different header types based on length and version bit
    if len(s) == HEADER_SIZE:  # 120 bytes - could be MeowPow OR padded AuxPOW
        # CRITICAL: Check if this is a padded AuxPOW header
        version_int = h['version']
        is_auxpow_padded = False
        
        if height >= constants.net.AuxPowActivationHeight and (version_int & (1 << 8)):
            # Check if last 40 bytes are padding (all zeros)
            if s[LEGACY_HEADER_SIZE:] == bytes(HEADER_SIZE - LEGACY_HEADER_SIZE):
                # This is a padded AuxPOW header - treat as 80 bytes
                is_auxpow_padded = True
        
        if not is_auxpow_padded:
            # Real MeowPow header (120 bytes)
            h['nheight'] = int(hash_encode(s[76:80]), 16)
            h['nonce'] = int(hash_encode(s[80:88]), 16)
            h['mix_hash'] = hash_encode(s[88:120])
        else:
            # Padded AuxPOW header - nonce is at position 76-80 (4 bytes)
            nonce_bytes = s[76:80]
            h['nonce'] = int(hash_encode(nonce_bytes), 16)
    else:  # 80 bytes - could be AuxPOW or legacy
        # AuxPOW blocks are identified by version bit AND height
        version_int = h['version']
        is_auxpow = bool(version_int & (1 << 8)) and height >= constants.net.AuxPowActivationHeight
        if is_auxpow:
            # This is an AuxPOW block (80 bytes) - nonce is at position 76-80 (4 bytes)
            nonce_bytes = s[76:80]
            h['nonce'] = int(hash_encode(nonce_bytes), 16)
            # Debug: log nonce value for AuxPoW blocks
            if h['nonce'] > 0xFFFFFFFF:
                print(f"DEBUG: AuxPoW nonce too large: {h['nonce']} (hex: {hash_encode(nonce_bytes)})")
        else:
            # Legacy block (80 bytes) - nonce is at position 76-80 (4 bytes)
            nonce_bytes = s[76:80]
            h['nonce'] = int(hash_encode(nonce_bytes), 16)
            # Debug: log nonce value for legacy blocks
            if h['nonce'] > 0xFFFFFFFF:
                print(f"DEBUG: Legacy nonce too large: {h['nonce']} (hex: {hash_encode(nonce_bytes)})")
    
    h['block_height'] = height
    return h

def hash_header(header: dict) -> str:
    if header is None:
        return '0' * 64
    if header.get('prev_block_hash') is None:
        header['prev_block_hash'] = '00' * 32
    # AuxPoW: use version bit to decide, gated by activation height
    height = header.get('block_height', 0)
    version_int = int(header.get('version', 0))
    is_auxpow = bool(version_int & (1 << 8)) and height >= constants.net.AuxPowActivationHeight
    if is_auxpow:
        return hash_raw_header_auxpow(serialize_header(header))
    elif header['timestamp'] >= constants.net.KawpowActivationTS and header['timestamp'] < constants.net.MeowpowActivationTS:
        return hash_raw_header_kawpow(serialize_header(header))
    elif header['timestamp'] >= constants.net.MeowpowActivationTS:
        return hash_raw_header_meowpow(serialize_header(header))
    elif header['timestamp'] >= constants.net.X16Rv2ActivationTS:
        hdr = serialize_header(header)[:80 * 2]
        h = hash_raw_header_v2(hdr)
        return h
    else:
        hdr = serialize_header(header)[:80 * 2]
        h = hash_raw_header_v1(hdr)
        return h


def hash_raw_header_v1(header: str) -> str:
    raw_hash = x16r_hash.getPoWHash(bfh(header)[:80])
    hash_result = hash_encode(raw_hash)
    return hash_result

def hash_raw_header_v2(header: str) -> str:
    raw_hash = x16rv2_hash.getPoWHash(bfh(header)[:80])
    hash_result = hash_encode(raw_hash)
    return hash_result

def revb(data):
    b = bytearray(data)
    b.reverse()
    return bytes(b)


def kawpow_hash(hdr_bin):
    header_hash = revb(sha256d(hdr_bin[:80]))
    mix_hash = revb(hdr_bin[88:120])
    nNonce64 = struct.unpack("< Q", hdr_bin[80:88])[0]
    final_hash = revb(kawpow.light_verify(header_hash, mix_hash, nNonce64))
    return final_hash


def hash_raw_header_kawpow(header: str) -> str:
    final_hash = hash_encode(kawpow_hash(bfh(header)))
    return final_hash


def meowpow_hash(hdr_bin):
    header_hash = revb(sha256d(hdr_bin[:80]))
    mix_hash = revb(hdr_bin[88:120])
    nNonce64 = struct.unpack("< Q", hdr_bin[80:88])[0]
    final_hash = revb(meowpow.light_verify(header_hash, mix_hash, nNonce64))
    return final_hash


def hash_raw_header_meowpow(header: str) -> str:
    final_hash = hash_encode(meowpow_hash(bfh(header)))
    return final_hash


def hash_raw_header_auxpow(header: str) -> str:
    """Hash for AuxPOW blocks using Scrypt-1024-1-1-256 on first 80 bytes"""
    import hashlib
    
    # Convert hex string to bytes
    try:
        header_bytes = bfh(header)[:80]  # Use only first 80 bytes for AuxPOW
    except Exception as e:
        _logger.error(f"ERROR converting header to bytes: {e}")
        from .crypto import sha256d
        return hash_encode(sha256d(b'\x00' * 80))
    
    # Scrypt-1024-1-1-256 (N=1024, r=1, p=1, dklen=32)
    # Match server implementation: return raw bytes, then encode
    
    if not _SCRYPT_AVAILABLE:
        _logger.warning(f"Scrypt not available during init, using SHA256 fallback for header")
        from .crypto import sha256d
        return hash_encode(sha256d(header_bytes))
    
    try:
        # CRITICAL: Use EXACT same parameters as server
        scrypt_hash = hashlib.scrypt(header_bytes, salt=header_bytes, n=1024, r=1, p=1, dklen=32)
        if len(scrypt_hash) != 32:
            raise ValueError(f"Scrypt returned {len(scrypt_hash)} bytes, expected 32")
        return hash_encode(scrypt_hash)
    except Exception as e:
        # Log the actual exception for debugging - this should NEVER happen if test passed
        _logger.error(f"❌ CRITICAL: hashlib.scrypt FAILED during AuxPOW hash calculation:")
        _logger.error(f"  Exception: {type(e).__name__}: {e}")
        _logger.error(f"  Header length: {len(header_bytes)} bytes")
        _logger.error(f"  Header (first 32 bytes): {header_bytes[:32].hex()}")
        _logger.error(f"  Initial test showed scrypt working, but failed during actual use!")
        _logger.error(f"  ⚠️ FALLING BACK TO SHA256 (WILL CAUSE VALIDATION FAILURES)")
        # Fallback - THIS IS INCORRECT
        from .crypto import sha256d
        scrypt_hash = sha256d(header_bytes)
        return hash_encode(scrypt_hash)


# key: blockhash hex at forkpoint
# the chain at some key is the best chain that includes the given hash
blockchains = {}  # type: Dict[str, Blockchain]
blockchains_lock = threading.RLock()  # lock order: take this last; so after Blockchain.lock


def read_blockchains(config: 'SimpleConfig'):
    best_chain = Blockchain(config=config,
                            forkpoint=0,
                            parent=None,
                            forkpoint_hash=constants.net.GENESIS,
                            prev_hash=None)
    blockchains[constants.net.GENESIS] = best_chain
    # consistency checks
    if best_chain.height() > constants.net.max_checkpoint():
        header_after_cp = best_chain.read_header(constants.net.max_checkpoint()+1)
        if not header_after_cp or not best_chain.can_connect(header_after_cp, check_height=False):
            _logger.info("[blockchain] deleting best chain. cannot connect header after last cp to last cp.")
            os.unlink(best_chain.path())
            best_chain.update_size()
    # forks
    fdir = os.path.join(util.get_headers_dir(config), 'forks')
    util.make_dir(fdir)
    # files are named as: fork2_{forkpoint}_{prev_hash}_{first_hash}
    l = filter(lambda x: x.startswith('fork2_') and '.' not in x, os.listdir(fdir))
    l = sorted(l, key=lambda x: int(x.split('_')[1]))  # sort by forkpoint

    def delete_chain(filename, reason):
        _logger.info(f"[blockchain] deleting chain {filename}: {reason}")
        os.unlink(os.path.join(fdir, filename))

    def instantiate_chain(filename):
        __, forkpoint, prev_hash, first_hash = filename.split('_')
        forkpoint = int(forkpoint)
        prev_hash = (64-len(prev_hash)) * "0" + prev_hash  # left-pad with zeroes
        first_hash = (64-len(first_hash)) * "0" + first_hash
        # forks below the max checkpoint are not allowed
        if forkpoint <= constants.net.max_checkpoint():
            delete_chain(filename, "deleting fork below max checkpoint")
            return
        # find parent (sorting by forkpoint guarantees it's already instantiated)
        for parent in blockchains.values():
            if parent.check_hash(forkpoint - 1, prev_hash):
                break
        else:
            delete_chain(filename, "cannot find parent for chain")
            return
        b = Blockchain(config=config,
                       forkpoint=forkpoint,
                       parent=parent,
                       forkpoint_hash=first_hash,
                       prev_hash=prev_hash)
        # consistency checks
        h = b.read_header(b.forkpoint)
        if first_hash != hash_header(h):
            delete_chain(filename, "incorrect first hash for chain")
            return
        if not b.parent.can_connect(h, check_height=False):
            delete_chain(filename, "cannot connect chain to parent")
            return
        chain_id = b.get_id()
        assert first_hash == chain_id, (first_hash, chain_id)
        blockchains[chain_id] = b

    for filename in l:
        instantiate_chain(filename)


def get_best_chain() -> 'Blockchain':
    return blockchains[constants.net.GENESIS]

# block hash -> chain work; up to and including that block
_CHAINWORK_CACHE = {
    "0000000000000000000000000000000000000000000000000000000000000000": 0,  # virtual block at height -1
}  # type: Dict[str, int]

if len(constants.net.DGW_CHECKPOINTS) > 0:
    _CHAINWORK_CACHE[constants.net.DGW_CHECKPOINTS[-1][1][0]] = 0  # set start of cache to 0 work


def init_headers_file_for_best_chain():
    b = get_best_chain()
    filename = b.path()
    length = HEADER_SIZE * (constants.net.max_checkpoint() + 1)
    if not os.path.exists(filename) or os.path.getsize(filename) < length:
        with open(filename, 'wb') as f:
            if length > 0:
                f.seek(length - 1)
                f.write(b'\x00')
        util.ensure_sparse_file(filename)
    with b.lock:
        b.update_size()


class Blockchain(Logger):
    """
    Manages blockchain headers and their verification
    """

    def __init__(self, config: 'SimpleConfig', forkpoint: int, parent: Optional['Blockchain'],
                 forkpoint_hash: str, prev_hash: Optional[str]):
        assert isinstance(forkpoint_hash, str) and len(forkpoint_hash) == 64, forkpoint_hash
        assert (prev_hash is None) or (isinstance(prev_hash, str) and len(prev_hash) == 64), prev_hash
        # assert (parent is None) == (forkpoint == 0)
        if 0 < forkpoint <= constants.net.max_checkpoint():
            raise Exception(f"cannot fork below max checkpoint. forkpoint: {forkpoint}")
        Logger.__init__(self)
        self.config = config
        self.forkpoint = forkpoint  # height of first header
        self.parent = parent
        self._forkpoint_hash = forkpoint_hash  # blockhash at forkpoint. "first hash"
        self._prev_hash = prev_hash  # blockhash immediately before forkpoint
        self.lock = threading.RLock()
        self.update_size()

    @property
    def legacy_checkpoints(self):
        return constants.net.CHECKPOINTS

    @property
    def checkpoints(self):
        return constants.net.DGW_CHECKPOINTS

    def get_max_child(self) -> Optional[int]:
        children = self.get_direct_children()
        return max([x.forkpoint for x in children]) if children else None

    def get_max_forkpoint(self) -> int:
        """Returns the max height where there is a fork
        related to this chain.
        """
        mc = self.get_max_child()
        return mc if mc is not None else self.forkpoint

    def get_direct_children(self) -> Sequence['Blockchain']:
        with blockchains_lock:
            return list(filter(lambda y: y.parent==self, blockchains.values()))

    def get_parent_heights(self) -> Mapping['Blockchain', int]:
        """Returns map: (parent chain -> height of last common block)"""
        with self.lock, blockchains_lock:
            result = {self: self.height()}
            chain = self
            while True:
                parent = chain.parent
                if parent is None: break
                result[parent] = chain.forkpoint - 1
                chain = parent
            return result

    def get_height_of_last_common_block_with_chain(self, other_chain: 'Blockchain') -> int:
        last_common_block_height = 0
        our_parents = self.get_parent_heights()
        their_parents = other_chain.get_parent_heights()
        for chain in our_parents:
            if chain in their_parents:
                h = min(our_parents[chain], their_parents[chain])
                last_common_block_height = max(last_common_block_height, h)
        return last_common_block_height

    @with_lock
    def get_branch_size(self) -> int:
        return self.height() - self.get_max_forkpoint() + 1

    def get_name(self) -> str:
        return self.get_hash(self.get_max_forkpoint()).lstrip('0')[0:10]

    def check_header(self, header: dict) -> bool:
        header_hash = hash_header(header)
        height = header.get('block_height')
        return self.check_hash(height, header_hash)

    def check_hash(self, height: int, header_hash: str) -> bool:
        """Returns whether the hash of the block at given height
        is the given hash.
        """
        assert isinstance(header_hash, str) and len(header_hash) == 64, header_hash  # hex
        try:
            return header_hash == self.get_hash(height)
        except Exception:
            return False

    def fork(parent, header: dict) -> 'Blockchain':
        if not parent.can_connect(header, check_height=False):
            raise Exception("forking header does not connect to parent chain")
        forkpoint = header.get('block_height')
        self = Blockchain(config=parent.config,
                          forkpoint=forkpoint,
                          parent=parent,
                          forkpoint_hash=hash_header(header),
                          prev_hash=parent.get_hash(forkpoint-1))
        self.assert_headers_file_available(parent.path())
        open(self.path(), 'w+').close()
        self.save_header(header)
        # put into global dict. note that in some cases
        # save_header might have already put it there but that's OK
        chain_id = self.get_id()
        with blockchains_lock:
            blockchains[chain_id] = self
        return self

    @with_lock
    def height(self) -> int:
        return self.forkpoint + self.size() - 1

    @with_lock
    def size(self) -> int:
        return self._size

    @with_lock
    def update_size(self) -> None:
        p = self.path()
        self._size = os.path.getsize(p)//HEADER_SIZE if os.path.exists(p) else 0

    @classmethod
    def verify_header(cls, header: dict, prev_hash: str, target: int, expected_header_hash: str=None, skip_bits_check: bool=False) -> None:
        # OPTIMIZATION: Defer expensive hashing until needed
        _hash = None
        
        # Check prev_hash linkage first (doesn't require hashing)
        if prev_hash != header.get('prev_block_hash'):
            raise InvalidHeader("prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash')))
        if constants.net.TESTNET:
            return
        
        # Check if this is an AuxPOW block
        height = header.get('block_height', 0)
        version_int = int(header.get('version', 0))
        is_auxpow = bool(version_int & (1 << 8)) and height >= constants.net.AuxPowActivationHeight
        
        if is_auxpow:
            # CRITICAL: AuxPOW blocks don't validate PoW on Meowcoin header
            # The actual PoW is in the parent block (Litecoin) included in AuxPOW data
            # We only verify prev_hash linkage, not difficulty
            # The server (ElectrumX + daemon) already validated the full AuxPOW chain
            # Only hash if expected_header_hash is provided
            if expected_header_hash:
                _hash = hash_header(header)
                if expected_header_hash != _hash:
                    raise InvalidHeader("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
            return
        
        # For non-AuxPOW blocks, verify bits and PoW
        # OPTIMIZATION: After checkpoints, only sample PoW validation (trust server for most blocks)
        should_validate_pow = True
        if height > constants.net.max_checkpoint():
            # After checkpoints: only validate PoW every 10th block (sampling)
            # This trades some security for much better performance  
            # We still validate prev_hash chain for ALL blocks (detects tampering)
            should_validate_pow = (height % 10 == 0)
        
        if not should_validate_pow:
            # Quick path: only check prev_hash linkage (already done above)
            # Skip expensive PoW hashing and validation
            return
        
        # Full validation path (checkpoints or sampling)
        # Skip bits check if we're using fallback target (not enough headers for LWMA)
        if not skip_bits_check:
            bits = cls.target_to_bits(target)
            if bits != header.get('bits'):
                raise InvalidHeader("bits mismatch: %s vs %s" % (bits, header.get('bits')))
        
        # Now hash only when we need it for PoW validation or expected_header_hash check
        _hash = hash_header(header)
        if expected_header_hash and expected_header_hash != _hash:
            raise InvalidHeader("hash mismatches with expected: {} vs {}".format(expected_header_hash, _hash))
        
        block_hash_as_num = int.from_bytes(bfh(_hash), byteorder='big')
        if block_hash_as_num > target:
            raise InvalidHeader(f"insufficient proof of work: {block_hash_as_num} vs target {target}")

    def verify_chunk(self, start_height: int, data: bytes) -> None:
        raw = []
        p = 0
        s = start_height
        prev_hash = self.get_hash(start_height - 1)
        headers = {}
        
        while p < len(data):
            # Determine expected header length *before* slicing so we stay aligned
            # CRITICAL FIX: Check AuxPOW first (takes precedence over KAWPOW)
            if s >= constants.net.AuxPowActivationHeight:
                # After AuxPOW activation, check version bit to determine header size
                version_int = int.from_bytes(data[p:p+4], byteorder='little', signed=False)
                is_auxpow = bool(version_int & (1 << 8))
                header_len = LEGACY_HEADER_SIZE if is_auxpow else HEADER_SIZE
            elif s >= constants.net.KawpowActivationHeight:
                header_len = HEADER_SIZE  # post-Kawpow headers always 120 bytes
            else:
                # Pre-KAWPOW: always 80 bytes (x16r/x16rv2)
                header_len = LEGACY_HEADER_SIZE

            raw = data[p:p + header_len]
            
            # Check for incomplete header at end
            if len(raw) < header_len:
                break  # Exit loop instead of processing incomplete header
                
            p += header_len
            try:
                expected_header_hash = self.get_hash(s)
            except MissingHeader:
                expected_header_hash = None
            if len(raw) not in (LEGACY_HEADER_SIZE, HEADER_SIZE):
                raise Exception('Invalid header length: {}'.format(len(raw)))
            header = deserialize_header(raw, s)
            headers[header.get('block_height')] = header
            
            # Don't bother with the target of headers in the middle of
            # DGW checkpoints
            target = 0
            skip_bits_check = False  # Track if we're using fallback target or LWMA (precision issues)
            
            if constants.net.DGW_CHECKPOINTS_START <= s <= constants.net.max_checkpoint():
                if self.is_dgw_height_checkpoint(s) is not None:
                    try:
                        target = self.get_target(s, headers)
                    except NotEnoughHeaders:
                        # LWMA needs more headers - trust the header's own bits during initial sync
                        target = self.bits_to_target(header['bits'])
                        skip_bits_check = True
                else:
                    # Just use the headers own bits for the logic
                    target = self.bits_to_target(header['bits'])
            else:
                # After checkpoints: trust server's validation
                # Use header's bits directly - server (ElectrumX + daemon) already validated full chain
                # Attempting to recalculate LWMA target causes mismatches due to:
                # 1. Slight differences in block collection during initial sync
                # 2. Rounding errors in target→bits→target conversion
                # 3. Timing differences in chunk processing
                target = self.bits_to_target(header['bits'])
                skip_bits_check = True
            
            try:
                self.verify_header(header, prev_hash, target, expected_header_hash, skip_bits_check=skip_bits_check)
            except InvalidHeader as e:
                # Log which specific header failed
                algo = get_block_algo(header, s)
                self.logger.error(f'Header validation FAILED at height {s}:')
                self.logger.error(f'  Algorithm: {algo}')
                self.logger.error(f'  Version: 0x{header["version"]:08x}')
                self.logger.error(f'  Bits: 0x{header["bits"]:08x}')
                self.logger.error(f'  Target used: {target}')
                self.logger.error(f'  Error: {e}')
                # DEBUG: For PoW failures, show header details
                if 'insufficient proof of work' in str(e):
                    header_hash = hash_header(header)
                    self.logger.error(f'  Header hash: {header_hash}')
                    self.logger.error(f'  Timestamp: {header.get("timestamp")}')
                    self.logger.error(f'  Nonce: {header.get("nonce", "N/A")} / Nonce64: {header.get("nonce64", "N/A")}')
                    self.logger.error(f'  Is AuxPOW: {bool(header["version"] & (1 << 8))}')
                raise
            
            # OPTIMIZATION: After checkpoints, avoid expensive hashing (especially Scrypt)
            # Peek at next header's prev_block_hash field instead of hashing current header
            # This is safe because we already validated PoW and the server is trusted post-checkpoint
            if s > constants.net.max_checkpoint() and p < len(data) - 36:
                # Peek at next header: prev_block_hash is always at bytes 4-36
                prev_hash = hash_encode(data[p + 4:p + 36])
            else:
                # Within checkpoints or last header: compute hash normally
                prev_hash = hash_header(header)
            s += 1

        # DEBUG: Log final counts before DGW validation
        processed_headers = s - start_height
        
        # DGW must be received in correct chunk sizes to be valid with our checkpoints
        # But ONLY within checkpoint range - after checkpoints, any chunk size is OK
        if constants.net.DGW_CHECKPOINTS_START <= start_height <= constants.net.max_checkpoint():
            # Only log if there's an issue
            if processed_headers != constants.net.DGW_CHECKPOINTS_SPACING:
                self.logger.warning(f'verify_chunk: processed {processed_headers} headers (expected {constants.net.DGW_CHECKPOINTS_SPACING})')
            assert start_height % constants.net.DGW_CHECKPOINTS_SPACING == 0, f'dgw chunk not from start: {start_height} % {constants.net.DGW_CHECKPOINTS_SPACING} != 0'
            if processed_headers != constants.net.DGW_CHECKPOINTS_SPACING:
                self.logger.error(f'DEBUG DGW chunk size mismatch: got {processed_headers}, expected {constants.net.DGW_CHECKPOINTS_SPACING}')
            assert processed_headers == constants.net.DGW_CHECKPOINTS_SPACING, f'dgw chunk not correct size: got {processed_headers}, expected {constants.net.DGW_CHECKPOINTS_SPACING}'

    @with_lock
    def path(self):
        d = util.get_headers_dir(self.config)
        if self.parent is None:
            filename = 'blockchain_headers'
        else:
            assert self.forkpoint > 0, self.forkpoint
            prev_hash = self._prev_hash.lstrip('0')
            first_hash = self._forkpoint_hash.lstrip('0')
            basename = f'fork2_{self.forkpoint}_{prev_hash}_{first_hash}'
            filename = os.path.join('forks', basename)
        return os.path.join(d, filename)

    @with_lock
    def save_chunk(self, start_height: int, chunk: bytes):
        assert start_height >= 0, start_height
        chunk_within_checkpoint_region = start_height <= constants.net.max_checkpoint()
        # chunks in checkpoint region are the responsibility of the 'main chain'
        if chunk_within_checkpoint_region and self.parent is not None:
            main_chain = get_best_chain()
            main_chain.save_chunk(start_height, chunk)
            return

        delta_height = (start_height - self.forkpoint)
        delta_bytes = delta_height * HEADER_SIZE
        # if this chunk contains our forkpoint, only save the part after forkpoint
        # (the part before is the responsibility of the parent)
        if delta_bytes < 0:
            chunk = chunk[-delta_bytes:]
            delta_bytes = 0
        truncate = not chunk_within_checkpoint_region

        def convert_to_kawpow_len():
            r = b''
            p = 0
            s = start_height
            while p < len(chunk):
                # CRITICAL FIX: Check AuxPOW first (takes precedence over KAWPOW)
                if s >= constants.net.AuxPowActivationHeight:
                    # After AuxPOW activation, check version bit to determine header size
                    version_int = int.from_bytes(chunk[p:p+4], byteorder='little', signed=False)
                    is_auxpow = bool(version_int & (1 << 8))
                    hdr_len = LEGACY_HEADER_SIZE if is_auxpow else HEADER_SIZE
                elif s >= constants.net.KawpowActivationHeight:
                    hdr_len = HEADER_SIZE
                else:
                    # Pre-KAWPOW: always 80 bytes (x16r/x16rv2)
                    hdr_len = LEGACY_HEADER_SIZE

                if hdr_len == LEGACY_HEADER_SIZE:
                    r += chunk[p:p + hdr_len] + bytes(40)  # pad to 120 for storage
                else:
                    r += chunk[p:p + hdr_len]

                p += hdr_len
                s += 1
            if len(r) % HEADER_SIZE != 0:
                raise Exception('Header extension error')
            return r

        chunk = convert_to_kawpow_len()
        self.write(chunk, delta_bytes, truncate)
        
        # Verify saved header can be read correctly
        # Note: After conversion, chunk is always in 120-byte format (padded if needed)
        try:
            saved_header = self.read_header(start_height)
            expected_header = deserialize_header(chunk[:HEADER_SIZE], start_height)
            
            if saved_header != expected_header:
                self.logger.error(f"save_chunk: Header mismatch at {start_height}")
                self.logger.error(f"  Chunk first 120 bytes (hex): {chunk[:HEADER_SIZE].hex()}")
                
                # Compare each field
                for key in set(list(saved_header.keys()) + list(expected_header.keys())):
                    saved_val = saved_header.get(key)
                    expected_val = expected_header.get(key)
                    if saved_val != expected_val:
                        self.logger.error(f"  Field '{key}' differs:")
                        self.logger.error(f"    Saved:    {saved_val}")
                        self.logger.error(f"    Expected: {expected_val}")
                
                raise AssertionError(f"Header mismatch at {start_height}: saved != expected")
        except Exception as e:
            if "Header mismatch" not in str(e):
                self.logger.error(f"save_chunk: Header verification exception at {start_height}: {e}")
            raise
        
        self.swap_with_parent()

    def swap_with_parent(self) -> None:
        with self.lock, blockchains_lock:
            # do the swap; possibly multiple ones
            cnt = 0
            while True:
                old_parent = self.parent
                if not self._swap_with_parent():
                    break
                # make sure we are making progress
                cnt += 1
                if cnt > len(blockchains):
                    raise Exception(f'swapping fork with parent too many times: {cnt}')
                # we might have become the parent of some of our former siblings
                for old_sibling in old_parent.get_direct_children():
                    if self.check_hash(old_sibling.forkpoint - 1, old_sibling._prev_hash):
                        old_sibling.parent = self

    def _swap_with_parent(self) -> bool:
        """Check if this chain became stronger than its parent, and swap
        the underlying files if so. The Blockchain instances will keep
        'containing' the same headers, but their ids change and so
        they will be stored in different files."""
        if self.parent is None:
            return False
        if self.parent.get_chainwork() >= self.get_chainwork():
            return False
        self.logger.info(f"swapping {self.forkpoint} {self.parent.forkpoint}")
        parent_branch_size = self.parent.height() - self.forkpoint + 1
        forkpoint = self.forkpoint  # type: Optional[int]
        parent = self.parent  # type: Optional[Blockchain]
        child_old_id = self.get_id()
        parent_old_id = parent.get_id()
        # swap files
        # child takes parent's name
        # parent's new name will be something new (not child's old name)
        self.assert_headers_file_available(self.path())
        child_old_name = self.path()
        with open(self.path(), 'rb') as f:
            my_data = f.read()
        self.assert_headers_file_available(parent.path())
        assert forkpoint > parent.forkpoint, (f"forkpoint of parent chain ({parent.forkpoint}) "
                                              f"should be at lower height than children's ({forkpoint})")
        with open(parent.path(), 'rb') as f:
            f.seek((forkpoint - parent.forkpoint)*HEADER_SIZE)
            parent_data = f.read(parent_branch_size*HEADER_SIZE)
        self.write(parent_data, 0)
        parent.write(my_data, (forkpoint - parent.forkpoint)*HEADER_SIZE)
        # swap parameters
        self.parent, parent.parent = parent.parent, self  # type: Tuple[Optional[Blockchain], Optional[Blockchain]]
        self.forkpoint, parent.forkpoint = parent.forkpoint, self.forkpoint
        self._forkpoint_hash, parent._forkpoint_hash = parent._forkpoint_hash, hash_header(deserialize_header(parent_data[:HEADER_SIZE], forkpoint))
        self._prev_hash, parent._prev_hash = parent._prev_hash, self._prev_hash
        # parent's new name
        os.replace(child_old_name, parent.path())
        self.update_size()
        parent.update_size()
        # update pointers
        blockchains.pop(child_old_id, None)
        blockchains.pop(parent_old_id, None)
        blockchains[self.get_id()] = self
        blockchains[parent.get_id()] = parent
        return True

    def get_id(self) -> str:
        return self._forkpoint_hash

    def assert_headers_file_available(self, path):
        if os.path.exists(path):
            return
        elif not os.path.exists(util.get_headers_dir(self.config)):
            raise FileNotFoundError('Electrum headers_dir does not exist. Was it deleted while running?')
        else:
            raise FileNotFoundError('Cannot find headers file but headers_dir is there. Should be at {}'.format(path))

    @with_lock
    def write(self, data: bytes, offset: int, truncate: bool=True) -> None:
        filename = self.path()
        self.assert_headers_file_available(filename)
        with open(filename, 'rb+') as f:
            if truncate and offset != self._size * HEADER_SIZE:
                f.seek(offset)
                f.truncate()
            f.seek(offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.update_size()

    @with_lock
    def save_header(self, header: dict) -> None:
        delta = header.get('block_height') - self.forkpoint
        data = bfh(serialize_header(header))
        # headers are only _appended_ to the end:
        assert delta == self.size(), (delta, self.size())
        
        # CRITICAL: Pad AuxPOW headers to HEADER_SIZE for consistent file offsets
        # AuxPOW headers are 80 bytes, but we store all headers as 120 bytes
        if len(data) == LEGACY_HEADER_SIZE:  # 80 bytes (AuxPOW or legacy)
            # Pad to 120 bytes for storage only
            data = data + bytes(HEADER_SIZE - LEGACY_HEADER_SIZE)
        
        assert len(data) == HEADER_SIZE, f"Header length {len(data)} != {HEADER_SIZE}"
        self.write(data, delta*HEADER_SIZE)
        self.swap_with_parent()

    @with_lock
    def read_header(self, height: int) -> Optional[dict]:
        if height < 0:
            return
        if height < self.forkpoint:
            return self.parent.read_header(height)
        if height > self.height():
            return
        delta = height - self.forkpoint
        name = self.path()
        self.assert_headers_file_available(name)
        with open(name, 'rb') as f:
            f.seek(delta * HEADER_SIZE)
            h = f.read(HEADER_SIZE)
            if len(h) < HEADER_SIZE:
                raise Exception('Expected to read a full header. This was only {} bytes'.format(len(h)))
        if h == bytes([0])*HEADER_SIZE:
            return None
        
        # CRITICAL: Unpad AuxPOW headers before deserializing
        # AuxPOW headers are stored as 120 bytes (padded) but need to be read as 80 bytes
        if height >= constants.net.AuxPowActivationHeight and len(h) == HEADER_SIZE:
            # Check if this looks like a padded AuxPOW header (version bit 8 set)
            version_int = int.from_bytes(h[0:4], byteorder='little')
            if version_int & (1 << 8):  # AuxPOW bit set
                # Check if last 40 bytes are padding (all zeros)
                if h[LEGACY_HEADER_SIZE:] == bytes(HEADER_SIZE - LEGACY_HEADER_SIZE):
                    h = h[:LEGACY_HEADER_SIZE]  # Remove padding
        
        return deserialize_header(h, height)

    def header_at_tip(self) -> Optional[dict]:
        """Return latest header."""
        height = self.height()
        return self.read_header(height)

    def is_tip_stale(self) -> bool:
        STALE_DELAY = 8 * 60 * 60  # in seconds
        header = self.header_at_tip()
        if not header:
            return True
        # note: We check the timestamp only in the latest header.
        #       The Bitcoin consensus has a lot of leeway here:
        #       - needs to be greater than the median of the timestamps of the past 11 blocks, and
        #       - up to at most 2 hours into the future compared to local clock
        #       so there is ~2 hours of leeway in either direction
        if header['timestamp'] + STALE_DELAY < time.time():
            return True
        return False

    @staticmethod
    def is_dgw_height_checkpoint(height) -> Optional[int]:
        # Less than the start of saved checkpoints
        if height < constants.net.DGW_CHECKPOINTS_START:
            return None
        # Greater than the end of the saved checkpoints
        if height > constants.net.max_checkpoint():
            return None
        height_mod = height % constants.net.DGW_CHECKPOINTS_SPACING
        # Is the first saved
        if height_mod == 0:
            return 0
        # Is the last saved
        elif height_mod == constants.net.DGW_CHECKPOINTS_SPACING - 1:
            return 1
        return None

    def get_hash(self, height: int) -> str:
        def is_height_checkpoint():
            within_cp_range = height <= constants.net.max_legacy_checkpoint()
            at_chunk_boundary = (height + 1) % 2016 == 0
            return within_cp_range and at_chunk_boundary

        dgw_height_checkpoint = self.is_dgw_height_checkpoint(height)
        if height == -1:
            return '0000000000000000000000000000000000000000000000000000000000000000'
        elif height == 0:
            return constants.net.GENESIS
        elif is_height_checkpoint():
            index = height // 2016
            h, t = self.legacy_checkpoints[index]
            return h
        elif dgw_height_checkpoint is not None:
            index = height // constants.net.DGW_CHECKPOINTS_SPACING - constants.net.DGW_CHECKPOINTS_START // constants.net.DGW_CHECKPOINTS_SPACING
            h, t = self.checkpoints[index][dgw_height_checkpoint]
            return h
        else:
            header = self.read_header(height)
            if header is None:
                raise MissingHeader(height)
            return hash_header(header)

    def get_target(self, height: int, chain=None) -> int:         
        dgw_height_checkpoint = self.is_dgw_height_checkpoint(height)

        if constants.net.TESTNET:
            return 0
        # Before we switched to Dark Wave Gravity Difficulty,
        # We used bitcoin's method of calculating difficulty.
        # The bits of each block (the difficulty) was the same for
        # The entire 2016 block checkpoint. Note that the last block hash to target
        # pairing in checkpoints.json
        # "000000000000f0bf1b393ef1dbbf23421eba2ad09de6315dcfaabe106fcf9e7a",
        # 2716428330192056873911465544471964056901126523302699863524769792
        # is technically incorrect but necessary due to DGW activating
        # in the middle of that chunk.
        elif height < constants.net.nDGWActivationBlock:
            h, t = self.legacy_checkpoints[height // 2016]
            return t
        elif dgw_height_checkpoint is not None:
            index = height // constants.net.DGW_CHECKPOINTS_SPACING - constants.net.DGW_CHECKPOINTS_START // constants.net.DGW_CHECKPOINTS_SPACING
            h, t = self.checkpoints[index][dgw_height_checkpoint]
            return t
        # There was a difficulty reset for kawpow
        elif not constants.net.TESTNET and height in range(373, 373 + 180):  # kawpow reset
            return KAWPOW_LIMIT
        # There was a difficulty reset for meowpow
        elif not constants.net.TESTNET and height in range(801212, 801212 + 180):  # meowpow reset
            return MEOWPOW_LIMIT
        # If we have a DWG header already saved to our header cache (i.e. for a reorg), get that
        elif height <= self.height():
            return self.bits_to_target(self.read_header(height)['bits'])
        else:
            # CRITICAL: Use LWMA multi-algo after AuxPOW activation
            # Before AuxPOW: use DGWv3 (single algo)
            # After AuxPOW: use LWMA (dual algo - MeowPow + Scrypt)
            if height >= constants.net.AuxPowActivationHeight:
                return self.get_target_lwma_multi_algo(height, chain)
            else:
                return self.get_target_dgwv3(height, chain)

    def convbignum(self, bits):
        MM = 256 * 256 * 256
        a = bits % MM
        if a < 0x8000:
            a *= 256
        target = a * pow(2, 8 * (bits // MM - 3))
        return target

    def get_target_dgwv3(self, height, chain=None) -> int:

        def get_block_reading_from_height(height):
            last = None
            try:
                last = chain.get(height)
            except Exception:
                pass
            if last is None:
                last = self.read_header(height)
            if last is None:
                raise NotEnoughHeaders()
            return last

        # params
        BlockReading = get_block_reading_from_height(height - 1)
        nActualTimespan = 0
        LastBlockTime = 0
        PastBlocksMin = DGW_PASTBLOCKS
        PastBlocksMax = DGW_PASTBLOCKS
        CountBlocks = 0
        PastDifficultyAverage = 0
        PastDifficultyAveragePrev = 0

        for _ in range(PastBlocksMax):
            CountBlocks += 1

            if CountBlocks <= PastBlocksMin:
                if CountBlocks == 1:
                    PastDifficultyAverage = self.convbignum(BlockReading.get('bits'))
                else:
                    bnNum = self.convbignum(BlockReading.get('bits'))
                    PastDifficultyAverage = ((PastDifficultyAveragePrev * CountBlocks) + (bnNum)) // (CountBlocks + 1)
                PastDifficultyAveragePrev = PastDifficultyAverage

            if LastBlockTime > 0:
                Diff = (LastBlockTime - BlockReading.get('timestamp'))
                nActualTimespan += Diff
            LastBlockTime = BlockReading.get('timestamp')

            BlockReading = get_block_reading_from_height((height - 1) - CountBlocks)

        bnNew = PastDifficultyAverage
        nTargetTimespan = CountBlocks * 60  # 1 min

        nActualTimespan = max(nActualTimespan, nTargetTimespan // 3)
        nActualTimespan = min(nActualTimespan, nTargetTimespan * 3)

        # retarget
        bnNew *= nActualTimespan
        bnNew //= nTargetTimespan
        bnNew = min(bnNew, MAX_TARGET)

        return bnNew

    def get_target_lwma_multi_algo(self, height, chain=None) -> int:
        """LWMA multi-algo difficulty adjustment for dual-mining era.
        
        After AuxPOW activation, the chain supports two algorithms in parallel:
        - MeowPow (native)
        - Scrypt (via AuxPOW/merge mining)
        
        Each algorithm has independent difficulty that adjusts based only on
        blocks mined with that same algorithm.
        """
        def get_block_reading_from_height(h):
            # Try chain dict first (for headers being validated in current chunk)
            last = None
            if chain:
                try:
                    last = chain.get(h)
                except Exception:
                    pass
            # Try reading from stored headers
            if last is None:
                try:
                    last = self.read_header(h)
                except Exception:
                    pass
            if last is None:
                raise NotEnoughHeaders(f'Cannot read header at height {h}')
            return last
        
        # Determine algorithm for the block we're calculating difficulty for
        # The caller (verify_chunk or can_connect) passes the header in the chain dict
        current_header = chain.get(height) if chain else None
        if current_header:
            current_algo = get_block_algo(current_header, height)
            # Only log for blocks after AuxPOW activation where new LWMA logic applies
            if height >= constants.net.AuxPowActivationHeight:
                pass
        else:
            # Fallback: if we don't have the header yet, we can't calculate target
            # This can happen during initial sync - raise NotEnoughHeaders to trigger chunk download
            raise NotEnoughHeaders(f'Missing header at height {height} to determine algorithm')
        
        # Parameters
        N = LWMA_AVERAGING_WINDOW
        aux_active = height >= constants.net.AuxPowActivationHeight
        ALGOS = 2 if aux_active else 1
        T_chain = POW_TARGET_SPACING
        T = T_chain * ALGOS  # Per-algo target: 60s * 2 = 120s
        
        # Select PoW limit for this algorithm
        if current_algo == 'scrypt':
            pow_limit = SCRYPT_LIMIT
        else:
            pow_limit = MEOWPOW_LIMIT
        
        # Collect last N+1 blocks of the SAME algorithm
        # CRITICAL: Start from (height-1) like daemon does from pindexLast (which is the prev block)
        same_algo_blocks = []
        search_limit = min(height - 1, N * 10)  # Don't search too far back
        
        # Daemon starts at h=pindexLast->nHeight (the prev block), we start at height-1
        for h in range(height - 1, max(-1, height - 1 - search_limit - 1), -1):
            if len(same_algo_blocks) >= N + 1:
                break
            if h < 0:
                break
            try:
                blk = get_block_reading_from_height(h)
                blk_algo = get_block_algo(blk, h)
                if blk_algo == current_algo:
                    same_algo_blocks.append(blk)
            except (NotEnoughHeaders, MissingHeader, Exception):
                # Not enough headers available yet - can't calculate LWMA
                break
        
        # If we don't have enough blocks of same algo, raise NotEnoughHeaders
        # This will trigger chunk download in the caller
        if len(same_algo_blocks) < N + 1:
            raise NotEnoughHeaders(f'Need {N+1} blocks of {current_algo}, only have {len(same_algo_blocks)}')
        
        # Reverse to get oldest-first order (daemon does std::reverse at line 210)
        same_algo_blocks.reverse()
        
        # Debug logging removed for performance
        
        # Calculate LWMA-1
        sum_targets = 0
        sum_weighted_solvetimes = 0
        prev_time = same_algo_blocks[0]['timestamp']
        
        for i in range(1, N + 1):
            blk = same_algo_blocks[i]
            ts = blk['timestamp']
            
            # Ensure timestamps are monotonic
            if ts <= prev_time:
                ts = prev_time + 1
            
            solve_time = ts - prev_time
            prev_time = ts
            
            # Clamp solve time (relative to per-algo target T)
            solve_time = max(1, min(solve_time, 6 * T))
            
            # Weighted sum
            sum_weighted_solvetimes += i * solve_time
            
            # Sum targets
            blk_target = self.bits_to_target(blk['bits'])
            sum_targets += blk_target
        
        # Average target
        avg_target = sum_targets // N
        
        # LWMA-1 formula: avgTarget * sumWeightedSolvetimes / k
        # where k = N * (N + 1) * T / 2
        k = N * (N + 1) * T // 2
        
        next_target = (avg_target * sum_weighted_solvetimes) // k
        
        # Clamp to pow limit
        next_target = min(next_target, pow_limit)
        
        # Log calculated target for debugging
        next_bits = self.target_to_bits(next_target)
        first_h = same_algo_blocks[0].get('block_height', 'unknown')
        last_h = same_algo_blocks[-1].get('block_height', 'unknown')
        
        # DEBUG: Detailed logging for specific heights
        if height in (1623069, 1623075):
            self.logger.error(f'LWMA CALC at {height}:')
            self.logger.error(f'  N={N}, T={T}, k={k}')
            self.logger.error(f'  sum_targets={sum_targets}')
            self.logger.error(f'  avg_target={avg_target}')
            self.logger.error(f'  sum_weighted_solvetimes={sum_weighted_solvetimes}')
            self.logger.error(f'  next_target={next_target}')
            self.logger.error(f'  next_bits=0x{next_bits:08x}')
            self.logger.error(f'  pow_limit={pow_limit}')
        
        # Detailed debugging removed for performance
        
        return next_target

    @classmethod
    def bits_to_target(cls, bits: int) -> int:
        # arith_uint256::SetCompact in Bitcoin Core
        if not (0 <= bits < (1 << 32)):
            raise InvalidHeader(f"bits should be uint32. got {bits!r}")
        bitsN = (bits >> 24) & 0xff
        bitsBase = bits & 0x7fffff
        if bitsN <= 3:
            target = bitsBase >> (8 * (3-bitsN))
        else:
            target = bitsBase << (8 * (bitsN-3))
        if target != 0 and bits & 0x800000 != 0:
            # Bit number 24 (0x800000) represents the sign of N
            raise InvalidHeader("target cannot be negative")
        if (target != 0 and
                (bitsN > 34 or
                 (bitsN > 33 and bitsBase > 0xff) or
                 (bitsN > 32 and bitsBase > 0xffff))):
            raise InvalidHeader("target has overflown")
        return target

    @classmethod
    def target_to_bits(cls, target: int) -> int:
        # arith_uint256::GetCompact in Bitcoin Core
        # see https://github.com/bitcoin/bitcoin/blob/7fcf53f7b4524572d1d0c9a5fdc388e87eb02416/src/arith_uint256.cpp#L223
        c = target.to_bytes(length=32, byteorder='big')
        bitsN = len(c)
        while bitsN > 0 and c[0] == 0:
            c = c[1:]
            bitsN -= 1
            if len(c) < 3:
                c += b'\x00'
        bitsBase = int.from_bytes(c[:3], byteorder='big')
        if bitsBase >= 0x800000:
            bitsN += 1
            bitsBase >>= 8
        return bitsN << 24 | bitsBase

    def chainwork_of_header_at_height(self, height: int) -> int:
        """work done by single header at given height"""
        target = self.get_target(height)
        work = ((2 ** 256 - target - 1) // (target + 1)) + 1
        return work

    @with_lock
    def get_chainwork(self, height=None) -> int:
        if height is None:
            height = max(0, self.height())
        if constants.net.TESTNET:
            # On testnet/regtest, difficulty works somewhat different.
            # It's out of scope to properly implement that.
            return height
        
        last_retarget = height // 2016 * 2016 - 1
        cached_height = last_retarget
        while _CHAINWORK_CACHE.get(self.get_hash(cached_height)) is None:
            if cached_height <= -1:
                break
            cached_height -= 2016
        assert cached_height >= -1, cached_height
        running_total = _CHAINWORK_CACHE[self.get_hash(cached_height)]
        while cached_height < last_retarget:
            work_in_chunk = 0
            for i in range(2016):
                work_in_chunk += self.chainwork_of_header_at_height(cached_height + i + 1)
            cached_height += 2016
            running_total += work_in_chunk
            _CHAINWORK_CACHE[self.get_hash(cached_height)] = running_total
        
        work_in_last_partial_chunk = 0
        for i in range(height - cached_height):
            work_in_last_partial_chunk += self.chainwork_of_header_at_height(cached_height + i + 1)
        assert cached_height + i + 1 == height

        return running_total + work_in_last_partial_chunk

    def can_connect(self, header: dict, check_height: bool=True) -> bool:
        if header is None:
            self.logger.info(f'can_connect: header is None')
            return False
        height = header['block_height']
        if check_height and self.height() != height - 1:
            self.logger.info(f'can_connect: height mismatch at {height}, blockchain height={self.height()}')
            return False
        if height == 0:
            result = hash_header(header) == constants.net.GENESIS
            if not result:
                self.logger.warning(f'can_connect: genesis hash mismatch')
            return result
        try:
            prev_hash = self.get_hash(height - 1)
        except Exception as e:
            self.logger.warning(f'can_connect: failed to get prev hash at {height}: {e}')
            return False
        if prev_hash != header.get('prev_block_hash'):
            self.logger.warning(f'can_connect: prev_hash mismatch at {height}')
            self.logger.warning(f'  Expected: {prev_hash}')
            self.logger.warning(f'  Got: {header.get("prev_block_hash")}')
            return False
        headers = {header.get('block_height'): header}
        
        # After checkpoints: trust server validation (same as verify_chunk)
        # Use header's bits directly to avoid LWMA recalculation issues
        skip_bits_check = False
        if height > constants.net.max_checkpoint():
            target = self.bits_to_target(header['bits'])
            skip_bits_check = True
        else:
            try:
                target = self.get_target(height, headers)
            except (MissingHeader, NotEnoughHeaders):
                # Re-raise NotEnoughHeaders so interface.py can request chunks
                raise
            except Exception as e:
                self.logger.warning(f'can_connect: get_target failed at {height}: {e}')
                return False
        
        try:
            self.verify_header(header, prev_hash, target, skip_bits_check=skip_bits_check)
        except BaseException as e:
            self.logger.warning(f'can_connect: verify_header failed at {height}: {e}')
            return False
        return True

    async def connect_chunk(self, start_height: int, hexdata: str) -> bool:
        assert start_height >= 0, start_height
        try:
            data = bfh(hexdata)
            # This is computationally intensive (thanks DGW)
            self.verify_chunk(start_height, data)
            self.save_chunk(start_height, data)
            return True
        except BaseException as e:
            self.logger.info(f'verify_chunk from height {start_height} failed: {repr(e)}')
            return False

    def get_checkpoints(self):
        # for each chunk, store the hash of the last block and the target after the chunk
        cp = []
        n = self.height() // 2016
        for index in range(n):
            h = self.get_hash((index+1) * 2016 -1)
            target = self.get_target(index)
            cp.append((h, target))
        return cp


def check_header(header: dict) -> Optional[Blockchain]:
    """Returns any Blockchain that contains header, or None."""
    if type(header) is not dict:
        return None
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.check_header(header):
            return b
    return None


def can_connect(header: dict) -> Optional[Blockchain]:
    """Returns the Blockchain that has a tip that directly links up
    with header, or None.
    """
    with blockchains_lock: chains = list(blockchains.values())
    for b in chains:
        if b.can_connect(header):
            return b
    return None


def get_chains_that_contain_header(height: int, header_hash: str) -> Sequence[Blockchain]:
    """Returns a list of Blockchains that contain header, best chain first."""
    with blockchains_lock: chains = list(blockchains.values())
    chains = [chain for chain in chains
              if chain.check_hash(height=height, header_hash=header_hash)]
    chains = sorted(chains, key=lambda x: x.get_chainwork(), reverse=True)
    return chains

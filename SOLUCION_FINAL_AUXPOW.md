# Solución Final: AuxPOW Multi-Algo en Meowcoin

## 🎯 Problema Original

```
ERROR: bits mismatch: 469825695 vs 460960622
```

## 🔍 Análisis del Problema

### El Error Fundamental

Después de investigación exhaustiva del código fuente de Meowcoin daemon, descubrimos que:

1. **Meowcoin usa DUAL MINING después del bloque 1,614,560**:
   - **MeowPow** (algoritmo nativo con ProgPow)
   - **AuxPOW/Scrypt** (merge mining con Litecoin)

2. **El daemon usa algoritmo LWMA (Linearly Weighted Moving Average)** multi-algo:
   - Cada algoritmo tiene **difficulty independiente**
   - Solo usa bloques **del mismo algoritmo** para calcular difficulty
   - Target spacing: **120 segundos por algoritmo** (60s × 2 algos)

3. **El cliente Electrum usaba DGWv3** (Dark Gravity Wave v3):
   - Usa **todos los bloques mezclados** sin distinguir algoritmo
   - Target spacing: **60 segundos total**
   - **INCOMPATIBLE** con dual mining

### Por Qué Fallaba

```
Cliente Electrum: Calcula difficulty con DGWv3 (todos los bloques mezclados)
                  → bits calculado: 469825695
                  
Daemon Meowcoin:  Calcula difficulty con LWMA (solo bloques mismo algoritmo)
                  → bits real: 460960622
                  
Resultado: MISMATCH → InvalidHeader
```

## ✅ Solución Implementada

### 1. **Algoritmo de Hashing (CORRECTO en ambos)**

**AuxPOW usa Scrypt-1024-1-1-256** (verificado en daemon `src/primitives/pureheader.cpp`):

```cpp
uint256 CPureBlockHeader::GetHash() const
{
    uint256 thash;
    scrypt_1024_1_1_256(BEGIN(nVersion), BEGIN(thash));
    return thash;
}
```

- ✅ **Servidor ElectrumX**: Usa Scrypt para AuxPOW
- ✅ **Cliente Electrum**: Usa Scrypt para AuxPOW
- ❌ **Error cometido**: Intentamos cambiar a SHA256 (incorrecto)
- ✅ **Revertido**: Ambos vuelven a usar Scrypt

### 2. **Algoritmo de Difficulty (IMPLEMENTADO LWMA)**

**Cliente Electrum ahora implementa LWMA multi-algo** (`electrum/blockchain.py`):

```python
def get_target_lwma_multi_algo(self, height, chain=None) -> int:
    """LWMA multi-algo difficulty adjustment for dual-mining era."""
    
    # Detect block algorithm (MeowPow vs Scrypt/AuxPOW)
    current_algo = get_block_algo(current_header, height)
    
    # Parameters
    N = 90  # LWMA averaging window
    ALGOS = 2  # Dual mining
    T = 60 * ALGOS  # 120s per algo
    
    # Collect last N+1 blocks of SAME algorithm only
    for h in range(height - 1, max(0, height - search_limit - 1), -1):
        blk = get_block_reading_from_height(h)
        if get_block_algo(blk, h) == current_algo:
            same_algo_blocks.append(blk)
    
    # Calculate LWMA-1: avgTarget * sumWeightedSolvetimes / k
    next_target = (avg_target * sum_weighted_solvetimes) // k
    
    return min(next_target, pow_limit)
```

### 3. **Detección de Algoritmo**

```python
def get_block_algo(header: dict, height: int) -> str:
    """Determine mining algorithm: 'scrypt' or 'meowpow'"""
    if height >= constants.net.AuxPowActivationHeight:
        version_int = header.get('version', 0)
        is_auxpow = bool(version_int & (1 << 8))
        return 'scrypt' if is_auxpow else 'meowpow'
    else:
        return 'meowpow'
```

### 4. **Límites PoW por Algoritmo**

```python
MEOWPOW_LIMIT = 0x0000000000ffffffffffffffffffffffffffffffffffffffffffffffffffffff
SCRYPT_LIMIT  = 0x00000fffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
```

## 📋 Estado Actual

### Servidor ElectrumX

**Estado**: Sincronizando desde bloque 0
**Problema**: Se detuvo en bloque 1,614,877 buscando hash incorrecto
**Solución**: Necesita limpieza completa de DB y resincronización

```bash
sudo systemctl stop electrumx
sudo rm -rf /var/lib/electrumx/history_db/ /var/lib/electrumx/utxo_db/ /var/lib/electrumx/meta/
sudo systemctl start electrumx
```

### Cliente Electrum

**Estado**: ✅ Código corregido con LWMA multi-algo
**Siguiente paso**: Compilar nuevo AppImage

```bash
cd /home/topper/Proyectos/electrum-meowcoin
./contrib/build-linux/appimage/build.sh
```

## 🚀 Resultado Esperado

Una vez que el servidor complete la resincronización (6-8 horas):

1. ✅ Servidor calcula hashes AuxPOW con **Scrypt** (correcto)
2. ✅ Servidor calcula difficulty con **LWMA multi-algo** (correcto)
3. ✅ Cliente calcula hashes AuxPOW con **Scrypt** (correcto)
4. ✅ Cliente calcula difficulty con **LWMA multi-algo** (correcto)
5. ✅ **bits match perfectamente** entre servidor y cliente
6. ✅ **No más errores de sincronización**

## 📊 Diferencias Clave: DGWv3 vs LWMA

| Aspecto | DGWv3 (Viejo) | LWMA Multi-Algo (Nuevo) |
|---------|---------------|-------------------------|
| Bloques usados | Últimos 180 (todos) | Últimos 90 (mismo algoritmo) |
| Target spacing | 60s total | 120s por algoritmo |
| Algoritmos | 1 (mezclado) | 2 (independientes) |
| Límites PoW | Uno global | Por algoritmo |
| Compatible con | Solo MeowPow | MeowPow + AuxPOW |

## 🔧 Commits Aplicados

**Cliente (`electrum-meowcoin`):**
- `3b022753a`: Implement LWMA multi-algo for AuxPOW era
- `d61b4d722`: Revert SHA256 fix (volver a Scrypt)

**Servidor (`electrumx-meowcoin`):**
- `c3f4daf3`: Revert SHA256 fix (volver a Scrypt)
- Todos los cambios previos de padding/unpadding de headers (correctos)

## ⚠️ Notas Importantes

1. **Scrypt es CORRECTO** para AuxPOW - verificado en código fuente del daemon
2. **LWMA es OBLIGATORIO** después de AuxPOW activation - no es opcional
3. **El servidor debe reindexar completamente** para eliminar metadata corrupta
4. **La compilación de AppImage falla** por problema de Docker (independiente del fix)




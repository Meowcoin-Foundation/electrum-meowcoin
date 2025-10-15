# 📋 INSTRUCCIONES PARA ACTUALIZAR CHECKPOINTS

## ✅ ESTADO ACTUAL
- ❌ Scripts innecesarios borrados 
- ✅ Script original `contrib/checkpoint_generator.py` listo
- ⚠️ Nodo Meowcoin no accesible desde aquí

---

## 🔄 OPCIONES PARA GENERAR CHECKPOINTS

### Opción A: Ejecutar en la máquina del nodo (RECOMENDADO)

1. **Copiar script a la máquina del nodo**:
```bash
# En la máquina del nodo Meowcoin
scp user@this-machine:/home/topper/Proyectos/electrum-meowcoin/contrib/checkpoint_generator.py .
```

2. **Ejecutar directamente en el nodo**:
```bash
# En la máquina donde corre el daemon Meowcoin
python3 checkpoint_generator.py <rpc_user> <rpc_password> [puerto]

# Ejemplos:
python3 checkpoint_generator.py meowcoinrpc mypassword123
python3 checkpoint_generator.py meowcoinrpc mypassword123 8766
```

3. **Copiar resultado de vuelta**:
```bash
# Se genera el archivo: checkpoints_dgw.json
# Copiarlo de vuelta a electrum-meowcoin/electrum/
scp checkpoints_dgw.json user@this-machine:/home/topper/Proyectos/electrum-meowcoin/electrum/
```

---

### Opción B: Configurar acceso RPC remoto

1. **En el nodo Meowcoin, editar `meowcoin.conf`**:
```ini
# Habilitar RPC
rpcuser=meowcoinrpc
rpcpassword=tu_password_seguro
rpcport=8766

# Permitir conexiones remotas (CUIDADO: solo redes seguras)
rpcallowip=192.168.1.0/24    # Tu red local
rpcbind=0.0.0.0:8766         # Escuchar en todas las interfaces
```

2. **Reiniciar daemon Meowcoin**

3. **Ejecutar desde aquí**:
```bash
cd /home/topper/Proyectos/electrum-meowcoin/contrib
python3 checkpoint_generator.py meowcoinrpc tu_password_seguro 8766
```

---

### Opción C: Usar SSH tunnel (SEGURO)

1. **Crear túnel SSH**:
```bash
# Desde esta máquina, crear túnel al nodo
ssh -L 8766:localhost:8766 user@nodo-meowcoin-ip
# Mantener esta terminal abierta
```

2. **En otra terminal, ejecutar script**:
```bash
cd /home/topper/Proyectos/electrum-meowcoin/contrib
python3 checkpoint_generator.py meowcoinrpc password 8766
```

---

## 📁 UBICACIONES DE ARCHIVOS

### Script generador:
```
/home/topper/Proyectos/electrum-meowcoin/contrib/checkpoint_generator.py
```

### Archivo destino:
```
/home/topper/Proyectos/electrum-meowcoin/electrum/checkpoints_dgw.json
```

### Configuración daemon (típica):
```
~/.meowcoin/meowcoin.conf
```

---

## ⚙️ CONFIGURACIÓN TÍPICA MEOWCOIN.CONF

```ini
# RPC Settings
rpcuser=meowcoinrpc
rpcpassword=tu_password_muy_seguro_aqui
rpcport=8766

# Para acceso local solamente (más seguro)
rpcallowip=127.0.0.1

# Para acceso en red local (menos seguro)
# rpcallowip=192.168.1.0/24
# rpcbind=0.0.0.0:8766

# Otros settings útiles
daemon=1
server=1
txindex=1
```

---

## 🚀 PROCESO COMPLETO PASO A PASO

### 1. Preparar acceso al nodo
```bash
# Opción más simple: ejecutar EN la máquina del nodo
ssh user@nodo-meowcoin
```

### 2. Verificar daemon funcionando
```bash
# En la máquina del nodo
meowcoin-cli getblockcount
# Debe devolver: 1624337 (o mayor)
```

### 3. Copiar script (si es necesario)
```bash
# Si ejecutas en máquina diferente
scp checkpoint_generator.py user@nodo:/tmp/
```

### 4. Ejecutar generación
```bash
# En la máquina del nodo
cd /tmp  # o donde copiaste el script
python3 checkpoint_generator.py meowcoinrpc password

# Salida esperada:
# Blocks: 1624337
# 2016
# 4032
# 6048
# ...
# Done.
```

### 5. Copiar resultado
```bash
# Se genera: checkpoints_dgw.json (archivo grande ~2MB)
# Copiarlo a electrum-meowcoin/electrum/
scp checkpoints_dgw.json user@dev-machine:/home/topper/Proyectos/electrum-meowcoin/electrum/
```

### 6. Verificar resultado
```bash
# De vuelta en la máquina de desarrollo
cd /home/topper/Proyectos/electrum-meowcoin
python3 -c "
import json
with open('electrum/checkpoints_dgw.json') as f:
    cp = json.load(f)
print(f'Checkpoints: {len(cp)}')
print(f'Coverage: ~{2016 + (len(cp)-1)*2016:,d} blocks')
"
```

---

## ⏱️ TIEMPO ESTIMADO

- **Conexión y setup**: 5 minutos
- **Generación checkpoints**: 15-30 minutos (805 checkpoints)
- **Copia archivos**: 1 minuto
- **Total**: ~30-40 minutos

---

## 🔧 TROUBLESHOOTING

### Error: "Connection refused"
```bash
# Verificar que daemon está corriendo
ps aux | grep meowcoin
# o
meowcoin-cli getinfo
```

### Error: "Authentication failed"
```bash
# Verificar credenciales en meowcoin.conf
cat ~/.meowcoin/meowcoin.conf | grep rpc
```

### Error: "Port not accessible"
```bash
# Verificar puerto correcto
netstat -tulpn | grep 8766
```

### Script toma mucho tiempo
```bash
# Es normal, está generando ~805 checkpoints
# Cada checkpoint requiere 2 llamadas RPC
# Total: ~1610 llamadas RPC
```

---

## 📊 RESULTADO ESPERADO

### Archivo generado:
- **Nombre**: `checkpoints_dgw.json`
- **Tamaño**: ~2-3 MB
- **Checkpoints**: ~805 checkpoints
- **Cobertura**: Desde altura 2,016 hasta ~1,624,337

### Próximo paso:
```bash
# Compilar nueva wallet con checkpoints actualizados
cd /home/topper/Proyectos/electrum-meowcoin
./contrib/build-linux/appimage/make_appimage.sh
```

---

## 🎯 COMANDOS COPY-PASTE

### Verificar nodo:
```bash
meowcoin-cli getblockcount
```

### Generar checkpoints:
```bash
python3 checkpoint_generator.py meowcoinrpc your_password 8766
```

### Verificar resultado:
```bash
ls -lh checkpoints_dgw.json
wc -l checkpoints_dgw.json
```

---

**IMPORTANTE**: El script tarda tiempo pero es normal. Cada checkpoint requiere consultar 2 bloques al daemon, así que para 805 checkpoints hace ~1610 consultas RPC.



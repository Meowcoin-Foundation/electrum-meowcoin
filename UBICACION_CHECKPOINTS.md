# 📍 UBICACIÓN DEL ARCHIVO CHECKPOINTS_DGW.JSON

## 🎯 UBICACIÓN CORRECTA

Coloca tu archivo `checkpoints_dgw.json` generado en:

```
/home/topper/Proyectos/electrum-meowcoin/electrum/checkpoints_dgw.json
```

## 📋 PASOS PARA REEMPLAZAR

### 1. Hacer backup del archivo actual (opcional)
```bash
cd /home/topper/Proyectos/electrum-meowcoin
cp electrum/checkpoints_dgw.json electrum/checkpoints_dgw.json.backup
```

### 2. Copiar tu nuevo archivo
```bash
# Si tu archivo está en el directorio actual:
cp checkpoints_dgw.json electrum/

# O si está en otro lugar:
cp /ruta/donde/generaste/checkpoints_dgw.json electrum/
```

### 3. Verificar que se copió correctamente
```bash
ls -lh electrum/checkpoints_dgw.json
# Debe mostrar un archivo más grande que el anterior (~2-3 MB)
```

### 4. Limpiar builds anteriores
```bash
rm -rf build/
rm -rf dist/
rm -rf contrib/build-linux/appimage/build/
```

### 5. Compilar nueva AppImage
```bash
./contrib/build-linux/appimage/make_appimage.sh
```

## 📊 VERIFICACIÓN

### Comparar tamaños:
```bash
# Archivo viejo (backup)
ls -lh electrum/checkpoints_dgw.json.backup

# Archivo nuevo 
ls -lh electrum/checkpoints_dgw.json
```

### El nuevo debe ser:
- **Tamaño**: ~2-3 MB (vs ~150KB del anterior)
- **Checkpoints**: ~805 checkpoints (vs ~405 del anterior)

---

## 🎯 RESPUESTA DIRECTA

**COLOCA EL ARCHIVO AQUÍ:**
```
electrum/checkpoints_dgw.json
```

**COMANDO DIRECTO:**
```bash
cp tu-archivo-generado.json electrum/checkpoints_dgw.json
```



# Antijira · MongoDB

Aplicación Python nativa con interfaz HTML, Bootstrap y JavaScript. **MongoDB es el almacén principal** para el registro, tablero, cronograma y edición. La aplicación ya no lee ni escribe tareas en Google Sheets durante su uso normal.

Se conservan la configuración de personas y estados, las categorías Desarrollo/Errores, la prioridad numérica decimal y las reglas actuales de planificación. No se requiere instalar MongoDB localmente si utilizas Atlas o un servidor existente.

## Acceso y administrador permanente

Después de configurar MongoDB, prepara la base y crea una única vez el administrador permanente:

```powershell
python app.py --init-db
python app.py --init-admin
```

El usuario inicial es `admin`; la contraseña se solicita de forma privada en la terminal y no se muestra ni se guarda en texto plano. El administrador permanente puede crear usuarios y administradores desde **Usuarios y auditoría**. La aplicación registra inicios y cierres de sesión, intentos fallidos, creación de usuarios, creación de tareas y modificaciones con el usuario responsable.

Las sesiones duran 12 horas. Cinco intentos fallidos bloquean temporalmente la cuenta durante 15 minutos.

## Conectar a Atlas o al servidor existente

Base de datos acordada: **antijira**.

1. En Atlas, usa **Connect → Drivers → Python** para obtener la URI.
2. Utiliza un usuario de base de datos con permiso `readWrite` sobre `antijira`. No es la cuenta de acceso al panel de Atlas.
3. Autoriza la IP del equipo que ejecuta Python en la lista de acceso de red de Atlas.
4. Copia `config/mongodb.example.json` a `config/mongodb.json` y reemplaza la URI. No guardes la contraseña en el código ni la envíes al navegador.

```json
{
  "uri": "mongodb+srv://USUARIO:CLAVE@TU_CLUSTER.mongodb.net/?retryWrites=true&w=majority",
  "database": "antijira",
  "timeout_ms": 5000
}
```

Para un servidor propio se admite también `mongodb://...` con sus opciones de autenticación y TLS. Usa la URI suministrada por tu administrador. Los caracteres especiales de usuario y contraseña deben estar codificados para URL.

Como alternativa, configura `ANTIJIRA_MONGODB_URI` y opcionalmente `ANTIJIRA_MONGODB_DATABASE` en el entorno del proceso. Las variables prevalecen sobre el JSON. No hay almacenamiento local alternativo si MongoDB no está disponible: la interfaz muestra el error de conexión.

El archivo privado está excluido de Git y el servidor Python no expone rutas de configuración. Como el proyecto reside en `htdocs`, conserva la denegación de acceso de Apache mediante el `.htaccess` existente o la configuración del VirtualHost; usa la variable de entorno si no quieres alojar una URI privada en esa carpeta.

## Instalar y ejecutar

PowerShell:

```powershell
cd C:\xampp\htdocs\contoda\Antijira
# Solo si todavía no existe el entorno:
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\mongodb.example.json config\mongodb.json
# Edita mongodb.json con tu conexión real antes de continuar.
.\.venv\Scripts\python.exe app.py --init-db
.\.venv\Scripts\python.exe app.py
```

No copies el ejemplo sobre una configuración privada que ya hayas completado. Abre **http://127.0.0.1:8085**. Si hay una versión anterior ejecutándose, detenla primero con Ctrl+C y vuelve a iniciarla.

`--init-db` comprueba la conexión y crea índices y contadores sin borrar tareas. Estos también se preparan en la primera operación de la aplicación. El driver oficial es PyMongo. No se necesita Apache ni MySQL.

## Categorías y parámetros

- `config/categories.json`: identificador, nombre, prefijo y cantidad mínima de dígitos por categoría.
- `config/parameters.json`: tus personas, estados, complejidades y demás parámetros existentes.
- `config/mongodb.json`: conexión privada; nunca se devuelve en `/api/config`.

Para agregar una categoría, agrega un elemento con identificador y prefijo únicos:

```json
{"id":"mejoras","name":"Mejoras","code_prefix":"MJ-","code_digits":3}
```

Pulsa Actualizar en la interfaz. Las categorías comparten una colección y se diferencian por el campo `category`; ya no corresponden a pestañas. Conserva identificadores y prefijos una vez utilizados. Retirar una categoría de la configuración no elimina sus tareas; pueden reasignarse a una categoría activa.

## Persistencia y concurrencia

Colecciones de `antijira`:

- `tasks`: documentos de tareas, categoría, código, prioridad Decimal128, duración, fechas, descripción, requerimiento, tiempo muerto, revisión e historial por usuario.
- `users`: usuarios con contraseña derivada mediante scrypt y roles `user` o `admin`.
- `sessions`: sesiones temporales con el token almacenado como hash y vencimiento automático.
- `audit`: registro consultable por administradores de las acciones y su responsable.
- `counters`: consecutivo independiente por categoría y contador global de identificadores.

El identificador permanece estable incluso al cambiar de categoría. El campo API `row` se conserva por compatibilidad con la interfaz, pero ahora representa ese identificador y no una fila de Excel.

Las vistas ordenan la prioridad numérica de mayor a menor. Solo se cargan las 10 tareas terminadas más recientes, junto con todas las tareas activas. El indicador violeta identifica las tareas marcadas como **Tiempo muerto**.

Al crear una tarea o cambiar su prioridad, responsable, inicio o duración, Antijira reprograma las tareas activas que le siguen al mismo responsable. La cola se ordena de mayor a menor prioridad y, en caso de empate, por antigüedad. Cada tarea posterior comienza el siguiente día laboral después de la entrega anterior; se excluyen sábados, domingos y festivos oficiales de Colombia. Las tareas terminadas y las fechas reales permanecen intactas. Cada movimiento automático queda registrado en el historial y en la auditoría.

El cronograma agrupa las tareas por responsable. Normalmente utiliza un solo carril y abre un segundo únicamente cuando hay intervalos superpuestos; nunca muestra más de dos. Los días laborales, fines de semana y festivos colombianos tienen fondos distintos.

Los filtros de responsable, estado y prioridad aceptan varias selecciones mediante casillas. El cronograma permite indicar libremente las fechas **Desde** y **Hasta**, o aplicar los periodos sugeridos de semana, mes y trimestre. Las flechas desplazan el intervalo completo anterior o siguiente.

Los filtros múltiples aparecen inicialmente con todos los valores seleccionados y cada lista incluye **Seleccionar todos** y **Borrar todos**. Las tareas marcadas como **Tiempo muerto** conservan sus propias fechas, pero se excluyen de la cadena automática: no consumen días ni desplazan las tareas posteriores.

Las fechas reales se registran automáticamente cuando un usuario cambia la tarea a un estado iniciado o terminado. Solo los administradores pueden corregir manualmente el inicio real y la finalización real; esta regla se valida en el servidor además de bloquear los campos en la interfaz.

Cada tarea admite hasta 25 adjuntos mediante enlaces. Cada adjunto requiere un nombre y una dirección completa `http://` o `https://`; los enlaces guardados pueden abrirse desde la edición de la tarea y su cantidad se muestra en el tablero y la tabla.

Los consecutivos se asignan mediante incrementos atómicos en MongoDB y un índice único protege los códigos. Una actualización exige la revisión vigente: si alguien ya modificó la tarea, devuelve conflicto 409 sin sobrescribirla. El cambio de categoría y código actualiza un único documento de forma atómica. No requiere un conjunto de réplicas para transacciones de varios documentos.

Puede haber saltos en la numeración si se reservó un número y luego falló la operación o hubo un conflicto. Los números no se reutilizan. Todas las instancias deben usar la misma configuración de categorías.

Una pérdida de conexión puede dejar incierto el resultado de una escritura. Revisa el tablero antes de repetir el registro. La asignación de códigos únicos no equivale a deduplicar dos envíos independientes del mismo formulario.

## Importación opcional desde Google Sheets

La hoja original y su configuración se conservan. **Nada se importa automáticamente** al arrancar el servidor.

Instala las dependencias opcionales de migración y comprueba:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-migration.txt
.\.venv\Scripts\python.exe migrate_sheets.py
```

Sin `--apply`, el comando consulta la hoja, comprueba duplicados y muestra el número de tareas pendientes. Puede preparar índices y contadores en MongoDB, pero no inserta tareas. Después, para importar:

```powershell
.\.venv\Scripts\python.exe migrate_sheets.py --apply
```

Se usa la configuración Google existente en `config/google_sheets.json`. Los identificadores de sus categorías deben existir en `config/categories.json`.

- Conserva códigos existentes, nombres, estados, prioridad y fechas.
- Guarda los encabezados y valores originales de cada fila en `legacy`, incluyendo columnas adicionales y tiempos históricos.
- Asigna un código a tareas antiguas sin código, después del máximo de su categoría.
- No interpreta horas antiguas como días ni recalcula fechas históricas.
- Rechaza códigos duplicados o incompatibles antes de insertar tareas.
- No modifica ni elimina datos en Google Sheets ni sobrescribe tareas existentes en MongoDB.
- Repetir la importación omite filas ya importadas. La identidad de origen incluye documento, pestaña y número de fila: no reordenes ni muevas filas del origen entre reintentos.
- Una importación interrumpida puede quedar parcial; vuelve a ejecutar el mismo comando para continuar. Se recomienda realizarla antes de comenzar a registrar tareas nuevas en MongoDB.

`antijira/sheets.py` y el servicio anterior quedan como adaptadores de compatibilidad para la importación y sus pruebas. `app.py` utiliza exclusivamente `MongoTaskService` y `MongoStore`.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
node --check static/app.js
```

Las pruebas nuevas usan `mongomock` solo como doble de MongoDB; nunca es un backend de la aplicación. Verifican persistencia en el repositorio simulado, 80 creaciones concurrentes, códigos independientes, prioridad decimal, control de revisión, traslado de categoría e importación idempotente. No sustituyen una prueba de integración contra Atlas; para comprobar conectividad y permisos reales utiliza `app.py --init-db` después de configurar la URI.

## Archivos principales

- `antijira/mongodb.py`: conexión, índices, contadores, persistencia, control de versión e importación.
- `antijira/mongo_service.py`: casos de uso con las validaciones actuales.
- `antijira/domain.py` y `antijira/calendar_co.py`: reglas del modelo y calendario.
- `app.py`: servidor HTTP y API.
- `migrate_sheets.py`: importación opcional desde Sheets.
- `templates/` y `static/`: interfaz actual conservada.
- `tests/test_mongodb.py`: pruebas del nuevo repositorio.

La aplicación mantiene el alcance de acceso actual: no incorpora autenticación de usuarios. Para acceso remoto público se necesita un despliegue con TLS y control de acceso.

Referencias: [conexión de aplicaciones a Atlas](https://www.mongodb.com/docs/atlas/driver-connection/), [atomicidad de MongoDB](https://www.mongodb.com/docs/manual/core/write-operations-atomicity/) y [documentación de PyMongo](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/).

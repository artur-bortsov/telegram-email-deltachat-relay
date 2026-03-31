# Aardvark — Inicio rápido en Windows

Cómo poner en marcha la retransmisión de canales de Telegram hacia Delta Chat y/o correo electrónico en pocos minutos.

---

## Qué se necesita

1. **El paquete Aardvark** — descargado y descomprimido en cualquier carpeta
2. **Credenciales de la API de Telegram** — API ID y API Hash
3. **Una cuenta de correo electrónico** — para envíos mediante Delta Chat o correo directo (si es necesario)
4. **Nombres de los canales de Telegram** — los canales que se desean retransmitir

---

## Paso 1 — Obtener las credenciales de la API de Telegram

1. Abra <https://my.telegram.org/apps> en un navegador
2. Inicie sesión con el número de teléfono de su cuenta de Telegram
3. Cree una aplicación (el nombre puede ser cualquiera, por ejemplo «Aardvark»)
4. Copie el **api_id** (un número) y el **api_hash** (una cadena de 32 caracteres)

---

## Paso 2 — Preparar una cuenta de correo electrónico *(si es necesario)*

Si se desea entrega mediante Delta Chat o correo electrónico directo, prepare una dirección
de correo dedicada exclusivamente al envío de mensajes de retransmisión.  
Se recomienda usar una dirección separada y no el buzón personal.

---

## Paso 3 — Ejecutar el instalador

Abra la carpeta donde descomprimió el paquete y haga doble clic en:

```
installers\windows\install.cmd
```

O ejecute desde el Símbolo del sistema (como administrador):

```cmd
installers\windows\install.cmd
```

El instalador abrirá un asistente de configuración interactivo.  Introduzca:

- **API ID** y **API Hash** (del paso 1)
- El número de teléfono de la cuenta de Telegram
- Los canales a retransmitir — por ejemplo `@channelname` o un ID numérico
- La configuración de Delta Chat y/o correo electrónico (si es necesario)

El resto de los parámetros puede dejarse con los valores predeterminados.

---

## Paso 4 — Confirmar el inicio de sesión en Telegram

En el primer arranque, Telegram envía un **código de verificación por SMS** al número de
teléfono indicado.  Introdúzcalo en la ventana del terminal del instalador.

Si la cuenta tiene activada la **verificación en dos pasos (Cloud Password / 2FA)**,
aparecerá una solicitud de contraseña inmediatamente después del código SMS.
Introdúzcala en el mismo terminal.

Tras un inicio de sesión correcto, la sesión se guarda en un archivo `.session`.
Los arranques posteriores del servicio lo utilizan automáticamente —
no es necesario introducir el código de nuevo.

---

## Paso 5 — Compartir los enlaces de invitación de Delta Chat

Tras el arranque del servicio, los enlaces de invitación para cada canal de Delta Chat
aparecen en:

```
C:\Program Files\Aardvark\invite_links.txt
```

Comparta estos enlaces con los suscriptores a través de un **canal seguro**
(por ejemplo, Signal o correo electrónico cifrado).
Los destinatarios deben abrir el enlace en la aplicación Delta Chat
para comenzar a recibir mensajes.

---

## Control del servicio

```cmd
sc query   AardvarkRelay
sc start   AardvarkRelay
sc stop    AardvarkRelay
```

Para la documentación completa consulte el [README principal](README.md).

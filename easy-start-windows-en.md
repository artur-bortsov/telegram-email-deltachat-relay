# Aardvark — Easy Start on Windows

Get Telegram channels relayed to Delta Chat and/or e-mail in a few minutes.

---

## What you need

1. **The Aardvark package** — downloaded and unzipped to any folder
2. **Telegram API credentials** — API ID and API Hash
3. **An e-mail account** — for Delta Chat delivery or plain e-mail forwarding (if needed)
4. **Telegram channel names** — the channels you want to relay

---

## Step 1 — Get Telegram API credentials

1. Open <https://my.telegram.org/apps> in a browser
2. Sign in with your Telegram phone number
3. Go to **API development tools**
4. Create an application (any name works, e.g. "Aardvark")
5. Copy the **api_id** (a number) and **api_hash** (a 32-character string)

---

## Step 2 — Prepare an e-mail account *(if needed)*

If you want Delta Chat or plain e-mail delivery, prepare a dedicated e-mail address
that will be used only for sending relay messages.  
A separate address is recommended — do not use a personal inbox.

---

## Step 3 — Run the installer

Open the folder where you unzipped the package and double-click:

```
installers\windows\install.cmd
```

Or run from a Command Prompt (as Administrator):

```cmd
installers\windows\install.cmd
```

The installer launches an interactive setup wizard.  Enter:

- **API ID** and **API Hash** (from step 1)
- Your Telegram phone number
- The channels to relay — e.g. `@channelname` or a numeric channel ID
- Delta Chat and/or e-mail relay settings (if needed)

All other settings can be left at the default values.

---

## Step 4 — Confirm the Telegram login

On the first run, Telegram sends an **SMS verification code** to the phone number
you entered.  Type it in the installer terminal window.

If your account has **two-step verification (Cloud Password / 2FA)** enabled,
a password prompt appears immediately after the SMS code.  Enter your password there.

After a successful login the session is saved to a `.session` file.
Future service starts use it automatically — no code is needed again.

---

## Step 5 — Share the Delta Chat invite links

After the service starts, invite links for each Delta Chat broadcast channel appear in:

```
C:\Program Files\Aardvark\invite_links.txt
```

Share these links with subscribers through a **secure channel**
(e.g. Signal or encrypted e-mail).
Recipients must open the link in the Delta Chat app to start receiving messages.

---

## Service control

```cmd
sc query   AardvarkRelay
sc start   AardvarkRelay
sc stop    AardvarkRelay
```

For full documentation see the [main README](README.md).

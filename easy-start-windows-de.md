# Aardvark — Schnellstart unter Windows

Telegram-Kanäle in wenigen Minuten an Delta Chat und/oder E-Mail weiterleiten.

---

## Was wird benötigt

1. **Das Aardvark-Paket** — heruntergeladen und in einen beliebigen Ordner entpackt
2. **Telegram-API-Zugangsdaten** — API-ID und API-Hash
3. **Ein E-Mail-Konto** — für die Zustellung über Delta Chat oder direkte E-Mail-Weiterleitung (falls gewünscht)
4. **Telegram-Kanalnamen** — die Kanäle, die weitergeleitet werden sollen

---

## Schritt 1 — Telegram-API-Zugangsdaten abrufen

1. Öffnen Sie <https://my.telegram.org/apps> in einem Browser
2. Melden Sie sich mit der Telefonnummer Ihres Telegram-Kontos an
3. Erstellen Sie eine Anwendung (beliebiger Name, z. B. „Aardvark“)
4. Kopieren Sie die **api_id** (eine Zahl) und den **api_hash** (eine 32-stellige Zeichenfolge)

---

## Schritt 2 — E-Mail-Konto vorbereiten *(falls benötigt)*

Wenn eine Zustellung über Delta Chat oder eine direkte E-Mail-Weiterleitung gewünscht ist,
wird eine dedizierte E-Mail-Adresse benötigt, die ausschließlich für den Versand von
Weiterleitungsnachrichten genutzt wird.  
Es wird empfohlen, eine separate Adresse zu verwenden und nicht das persönliche Postfach.

---

## Schritt 3 — Installer ausführen

Öffnen Sie den Ordner, in den Sie das Paket entpackt haben, und doppelklicken Sie auf:

```
installers\windows\install.cmd
```

Oder führen Sie es über die Eingabeaufforderung aus (als Administrator):

```cmd
installers\windows\install.cmd
```

Der Installer startet einen interaktiven Einrichtungsassistenten.  Geben Sie ein:

- **API-ID** und **API-Hash** (aus Schritt 1)
- Die Telefonnummer des Telegram-Kontos
- Die weiterzuleitenden Kanäle — z. B. `@kanalname` oder eine numerische Kanal-ID
- Delta-Chat- und/oder E-Mail-Einstellungen (falls benötigt)

Alle übrigen Einstellungen können auf den Standardwerten belassen werden.

---

## Schritt 4 — Telegram-Anmeldung bestätigen

Beim ersten Start sendet Telegram einen **SMS-Bestätigungscode** an die eingetragene
Telefonnummer.  Geben Sie diesen im Terminalfenster des Installers ein.

Wenn für das Konto die **Zwei-Schritt-Verifizierung (Cloud-Passwort / 2FA)** aktiviert ist,
erscheint unmittelbar nach dem SMS-Code eine Passwortabfrage —
geben Sie Ihr Passwort dort ein.

Nach erfolgreicher Anmeldung wird die Sitzung in einer `.session`-Datei gespeichert.
Beim nächsten Start des Dienstes wird sie automatisch verwendet —
eine erneute Code-Eingabe ist nicht erforderlich.

---

## Schritt 5 — Delta-Chat-Einladungslinks teilen

Nach dem Start des Dienstes erscheinen Einladungslinks für jeden Delta-Chat-Broadcast-Kanal in:

```
C:\Program Files\Aardvark\invite_links.txt
```

Teilen Sie diese Links über einen **sicheren Kanal** mit Abonnenten
(z. B. Signal oder verschlüsselte E-Mail).
Die Empfänger müssen den Link in der Delta-Chat-App öffnen,
um Nachrichten zu empfangen.

---

## Dienstverwaltung

```cmd
sc query   AardvarkRelay
sc start   AardvarkRelay
sc stop    AardvarkRelay
```

Die vollständige Dokumentation finden Sie in der [Haupt-README](README.md).

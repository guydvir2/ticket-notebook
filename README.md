# Ticket Notebook

A logbook for home fixes. Python standard library only, no packages to install.

## Run it

    python3 app.py

In PyCharm, pick **Ticket Notebook** next to the Run button. It comes from the
`.run` folder, so it survives every pull. If PyCharm asks for an interpreter,
choose any Python 3 once (no packages needed).

Then open `http://<this-machine-ip>:8080` from any device on your home network.

On the iPhone: open that address in Safari, tap Share, then **Add to Home Screen**.
It opens without the Safari bars and uses its own icon.

## Using it

- Each card shows its serial number, section, opening date, title and the first
  line of the problem. The serial is given on saving and never changes, so
  numbers have gaps once you delete a ticket.
- Search also matches a serial, so `#007` finds that ticket.
- The three counts at the top filter by status. Tap one, tap again to clear.
- Tap a ticket to read it. Change status straight from that screen.
- **Copy for chat** puts the ticket back in template form, ready to paste into a chat.
- **paste** takes an error, a log or part of a chat. The AI writes the ticket and
  opens it for review before saving. A filled template is read directly, no AI needed.
- Wrap paths and commands in `backticks` to have them shown as code.

## Secret notes

- Every ticket has a **Secret note** field for passwords, keys and IPs. The list only
  shows a lock; tap **show** inside the ticket to read it. It is never part of
  **Copy for chat** and never sent to the AI.
- **vault** (top right) holds secret notes that belong to no ticket, sorted into
  notebooks. Tap **+ Notebook** to add one; old notes land in **General**.
- Both are stored encrypted in `tickets.db`. The key is made on first run in
  `secret.key` next to `app.py`; git ignores it.
- **Back up `secret.key` together with `tickets.db`.** Without the key the secret notes
  cannot be read. Anyone who can open the app can read them, so keep the app on your
  home network.

## AI writer (OmniRoute)

Copy `.env.example` to `ai.env` next to `app.py` and fill in your OmniRoute address,
API key and combo. `ai.env` is ignored by git, so the key stays off GitHub.
Restart the app; its start message says `AI writer: on`.

## Data

Everything lives in `tickets.db` next to `app.py`. Back that file up, and `secret.key` with it.
Copy it elsewhere to move the app.

## Keep it at home

There is no login. Use it on your home network only, never expose the port to
the internet, and do not store passwords or keys in tickets.

## Start at boot (Debian or Ubuntu, files in /opt/ticket-notebook)

    cp ticket-notebook.service /etc/systemd/system/
    systemctl daemon-reload && systemctl enable --now ticket-notebook

Change the port with `TN_PORT`, the database path with `TN_DB`.

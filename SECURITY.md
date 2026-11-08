# Security

Browser Backup Manager protects your backups with strong encryption, but the **optional app
lock is only a basic lock**. Please read this page so you know exactly what is and is not
protected.

## TL;DR

| Part | How strong is it? |
|---|---|
| **Backup files** (`.bbvault`) | Strong. AES-256-GCM, key from your backup password via scrypt. Only as strong as the password you choose. |
| **App lock** (optional) | **Basic. Not 100% secure and it can be bypassed.** It is a convenience gate on the app's window, not protection for your data. |

## The app lock is basic, and can be bypassed

The app lock exists to stop someone casually opening the app on your PC and clicking
**Reset browser** or **Delete backup file**. It is **not** a vault, and it will not stop a
determined person or a program running as you.

Known ways to bypass it (this list is not complete):

- **Delete `lock.json`.** The lock is on exactly when `%APPDATA%\BrowserBackupManager\lock.json`
  exists, so removing the file turns the lock off. This is also the official way to reset a
  forgotten app password.
- **Edit `lock.json` or `config.json`.** The wrong-attempt counter and the wait time are stored in
  `lock.json`, so they can be reset by hand. The "lock is off" flag in `config.json` can be edited
  too.
- **Run the source code directly.** The lock is checked by the GUI. The backup and restore code
  (`browser_backup/`) does not need the app password, so anyone with Python can call it without
  ever seeing the lock screen.
- **Replace or modify the app.** Nothing signs or verifies the program, so the EXE or the source
  can be swapped for a version without the lock.
- **Read the app's memory or attach a debugger** while it is running or unlocked.
- **Use the app while it is unlocked.** If you step away from an unlocked window, anyone can use
  it until the 10-minute idle lock kicks in. Use **Lock now** when leaving your PC.
- **Crack the hash offline.** If someone copies `lock.json`, they can try passwords against it
  without the in-app delays. The password is stored as a salted PBKDF2-SHA256 hash (600,000
  iterations), and the minimum length is only 8 characters, so a short or common password can be
  guessed.

What this means in practice:

- Anyone who can log in to **your Windows account** (or run programs as you) can get past the lock.
- The lock **does not encrypt anything**. It does not protect your browser profile, your backup
  files or the backup folder.
- The lock password is **separate** from your backup passwords. Knowing one tells you nothing about
  the other.
- Don't rely on the app lock for anything sensitive. For real protection use a Windows account
  password, full-disk encryption such as BitLocker, and a strong backup password.

## What is actually protected

### Backup files

- AES-256-GCM authenticated encryption, in chunks, with a key derived from your password using
  scrypt (N=2^17, r=8, p=1, about 128 MiB of memory per guess).
- Tampered, reordered or truncated files fail authentication and are rejected before your browser
  data is touched.
- The backup password is never stored anywhere: not in the backup, the `.meta.json` or the
  settings.
- **There is no password recovery.** Lose the password and the backup is gone.
- A backup is only as strong as its password. The app rejects short and obviously guessable
  passwords, but that check is a rough estimate, not a guarantee. A long passphrase of several
  unrelated words is best.

### What is NOT encrypted

- The `.meta.json` next to each backup (browser, backup name, date, file count) is plain text.
  The file size is also visible. Don't put secrets in a backup's name.
- If you choose to keep the previous browser data after a restore, that copy
  (`.before-restore-...` folder) is **unencrypted**. Delete it once you have checked the result.
- Your live browser profile is as protected as the browser makes it. After a restore, the data is
  back on disk in the browser's normal, browser-protected form.
- If your backup folder is synced by OneDrive, Dropbox, Google Drive and similar, the encrypted
  file and its `.meta.json` are uploaded. The app warns you about this.

### Browser-specific notes

- Chromium browsers (Brave, Chrome, Edge, Vivaldi, Opera, Opera GX) tie cookies and saved
  passwords to your Windows account. A restore on another account or PC may require logging in
  again. Chrome and Edge also use app-bound encryption, so test a same-PC restore before you rely
  on it.
- Firefox backups are portable.

## The program itself

- The EXE is **not code-signed**. Windows SmartScreen may warn, and some antivirus engines may
  flag it as a false positive. The full source is in this repository; you can build the EXE
  yourself (see the README) and compare SHA-256 hashes with the release notes.
- The app force-closes browsers and can delete browser profiles by design. Always make a test
  backup and restore before relying on it.

## Not in scope

This project does not claim to defend against: malware already running on your PC, someone with
administrator or full account access, keyloggers, or physical access to an unlocked machine.

## Reporting a vulnerability

Found a security problem? Please **don't** open a public issue with exploit details. Use GitHub's
**Security → Report a vulnerability** (private advisory) on this repository, or contact the
maintainer privately through the contact method on their GitHub profile. Include the version, steps
to reproduce and the impact. Bypassing the app lock is a *known limitation* described above, so
that alone is not a vulnerability; flaws in the backup encryption, file handling or restore logic
are.

## Disclaimer

This software is provided "as is", without warranty (see [LICENSE](LICENSE)). You use it at your own
risk.

# Slex Launcher

A small personal Minecraft: Java Edition launcher written in Python (CustomTkinter).
It is a hobby project for my own use. It is not sold, not monetized and not distributed commercially.

Kişisel kullanım için Python ile yazılmış küçük bir Minecraft: Java Edition launcher'ıdır.
Hobi projesidir, satılmaz, para kazanılmaz.

## Features / Özellikler

- Vanilla, Fabric and Forge support
- Snapshot versions
- Separate profiles with their own mods / saves / config folders
- Mod and resource pack search and download via the Modrinth API
- Microsoft account sign-in (OAuth 2.0 with PKCE, `XboxLive.signin` scope, `consumers` tenant)
- Offline mode for local play

## Requirements / Gereksinimler

- Python 3
- Java 17 or 21 (depending on the Minecraft version)
- `py -m pip install -U customtkinter minecraft-launcher-lib certifi`

## Run / Çalıştırma

```
py launcher.py
```

## Notes / Notlar

- The launcher stores its data in its own folder (`%APPDATA%\.basit_launcher`) and does not touch the official `.minecraft` folder.
- The Azure client ID in the code is a public client ID (no client secret is used).
- This project is not affiliated with Mojang or Microsoft.

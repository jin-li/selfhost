# MusicBrainz Picard

Community `jlesage/musicbrainz-picard` image: the Python GUI runs inside the
container and is reached through its web desktop. Settings persist in
`${CONFIG_DIR}`, working copies in `${STAGING_DIR}`, and the music library is
mounted read-only at `/storage/library` from `${MUSIC_DIR}`.

## CJK fonts

The base image only ships Latin fonts, so Chinese/Japanese/Korean text in the
desktop renders as boxes. Fix it by bind-mounting a font directory that
contains Noto CJK fonts (for example
`NotoSansCJK-VF.otf.ttc`) at `/usr/share/fonts/host-cjk`:

```yaml
services:
  picard:
    volumes:
      - ${FONT_DIR}:/usr/share/fonts/host-cjk:ro
```

Declare `FONT_DIR` in the service manifest and host inventory, and add the
mount through a host overlay (`compose.hostOverlay`), as
`local/t460s/picard/compose.t460s.yaml` does. Fontconfig picks the mounted
fonts up without a cache rebuild; verify with
`docker exec picard fc-list :lang=zh`.

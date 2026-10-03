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

Set `FONT_DIR` in your local environment and add this mount in a Compose
override file. Include that file in each Compose invocation. Fontconfig picks
the mounted fonts up without a cache rebuild; verify with
`docker exec picard fc-list :lang=zh`.

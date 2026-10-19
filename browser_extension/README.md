# Job Beast Companion

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this directory.
4. Run `jobbeast apply JOB_ID --dry-run` or `--submit`.

The extension communicates only with the foreground Job Beast bridge at
`127.0.0.1:8765` and activates only when the current URL matches the active
application packet.

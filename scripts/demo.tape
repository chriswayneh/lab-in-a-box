Output demo.gif
Set FontSize 14
Set Width 1100
Set Height 720
Set Theme "Catppuccin Mocha"
Set FontFamily "JetBrains Mono"
Set TypingSpeed 18ms
Set Framerate 24
Set Padding 16
Set WindowBar Colorful
Set Shell bash
Hide
Type `export PATH="$LAB_BIN_DIR:$PATH"`
Enter
Type `cd "$LAB_DEMO_HOME"`
Enter
Type `export PS1='demo$ '`
Enter
Type `clear`
Enter
Show

Type `make help | sed -n '1,28p'`
Enter
Sleep 2.2s

Type `docker compose -f docker-compose.yml config --quiet && echo 'compose: OK (client-side, no daemon required for config)'`
Enter
Sleep 1.6s

Type `python3 -m pytest -q -p no:cacheprovider scripts/identity/unit`
Enter
Sleep 2.8s

Sleep 1.2s

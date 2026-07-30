Set objShell = CreateObject("WScript.Shell")
exitCode = objShell.Run("cmd /c cd /d ""C:\Program Files (x86)\cloudflared"" && cloudflared.exe tunnel --config ""C:\Users\USER\.cloudflared\config.yml"" run", 0, True)
WScript.Quit(exitCode)

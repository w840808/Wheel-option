Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "e:\coding 練習\sell put and covered call"
WshShell.Run "pythonw tg_bot.py", 0, False

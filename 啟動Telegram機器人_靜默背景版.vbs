Set WshShell = CreateObject("WScript.Shell")
' 獲取當前腳本所在目錄
strDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
' 靜默無黑窗運行 pythonw tg_bot.py
WshShell.CurrentDirectory = strDir
WshShell.Run "pythonw tg_bot.py", 0, False

Option Explicit
Dim shell, fso, scriptDir, watchdog, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
watchdog = fso.BuildPath(scriptDir, "rdc-watchdog.ps1")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & watchdog & """"
WScript.Quit shell.Run(command, 0, True)

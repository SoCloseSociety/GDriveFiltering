' Double-click this to launch GDriveFiltering with NO console window.
' Right-click -> Send to -> Desktop (create shortcut) to put it on your Desktop.
Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
shell.Run """" & here & "\GDriveFiltering.bat""", 0, False

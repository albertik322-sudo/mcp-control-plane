Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
runner = fso.BuildPath(root, "context_collector_task.py")

shell.CurrentDirectory = root
shell.Run """" & pythonw & """ """ & runner & """", 0, False

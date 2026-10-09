
' 投资仪表盘 一键启动(共享版)
' 双击本文件即可: 后台起服务(无窗口) -> 等就绪 -> 自动打开浏览器。
Option Explicit
Dim sh, fso, dir, q
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
q = Chr(34)
' 0 = 隐藏窗口, False = 不等它结束(立刻返回)
sh.Run q & dir & "\启动投资仪表盘.bat" & q, 0, False

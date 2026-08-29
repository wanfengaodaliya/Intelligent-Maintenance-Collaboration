from pathlib import Path
import re


SCRIPT = (Path(__file__).parents[3] / "start_project.ps1").read_text(encoding="utf-8-sig")


def test_host_process_detection_uses_parent_child_ownership():
    assert re.search(r"\[int\]\$ProcessId,\s*\[string\]\$Module", SCRIPT)
    assert "ParentProcessId" in SCRIPT
    assert "-ExpectedParentPid $parentPid" in SCRIPT
    assert "[regex]::Escape($Module) + ':app" in SCRIPT
    assert "@('powershell.exe', 'pwsh.exe')" in SCRIPT
    assert "for ($depth = 0; $depth -lt 6" in SCRIPT
    assert "$ancestorPid -eq $ExpectedParentPid" in SCRIPT
    assert '$expectedProjectLocation = "Set-Location \'$CloudEdge\'"' in SCRIPT
    assert "$ancestorCmd.IndexOf($expectedProjectLocation" in SCRIPT
    assert "$ancestorCmd.IndexOf($CloudEdge" not in SCRIPT
    assert "param([int]$Pid, [string]$Module)" not in SCRIPT


def test_port_detection_includes_wildcard_bindings():
    assert "Get-NetTCPConnection -State Listen -LocalPort $Port" in SCRIPT
    assert "-LocalAddress $HostName" not in SCRIPT

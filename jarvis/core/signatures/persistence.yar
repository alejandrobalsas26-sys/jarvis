rule Persistence_Registry
{
    meta:
        description = "Registry run-key based persistence"
        mitre_technique = "T1547.001"
    strings:
        $r1 = "reg add" nocase ascii
        $r2 = "CurrentVersion\\Run" nocase ascii
        $r3 = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" nocase ascii
        $r4 = "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" nocase ascii
    condition:
        any of them
}

rule Persistence_ScheduledTask
{
    meta:
        description = "Scheduled task creation for persistence"
        mitre_technique = "T1053.005"
    strings:
        $s1 = "schtasks" nocase ascii
        $s2 = "/create" nocase ascii
        $s3 = "New-ScheduledTask" nocase ascii
        $s4 = "Register-ScheduledTask" nocase ascii
    condition:
        ($s1 and $s2) or $s3 or $s4
}

rule Persistence_StartupFolder
{
    meta:
        description = "Startup folder file drop"
        mitre_technique = "T1547.001"
    strings:
        $f1 = "\\Microsoft\\Windows\\Start Menu\\Programs\\Startup" nocase ascii
        $f2 = "AppData\\Roaming\\Microsoft\\Windows\\Start Menu" nocase ascii
    condition:
        any of them
}

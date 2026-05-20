rule Evasion_EncodedCommand
{
    meta:
        description = "PowerShell encoded command execution"
        mitre_technique = "T1059.001"
    strings:
        $enc1 = "-encodedcommand" nocase ascii
        $enc2 = "frombase64string" nocase ascii
        $enc3 = "-bypass" nocase ascii
        $enc4 = "-windowstyle hidden" nocase ascii
    condition:
        any of them
}

rule Evasion_CertutilDecode
{
    meta:
        description = "Certutil file decode — living-off-the-land binary abuse"
        mitre_technique = "T1027"
    strings:
        $c1 = "certutil" nocase ascii
        $c2 = "-decode" nocase ascii
    condition:
        all of them
}

rule Evasion_ObfuscatedChars
{
    meta:
        description = "Character-level command obfuscation indicators"
        mitre_technique = "T1027"
    strings:
        $o1 = "^e^c^h^o" nocase ascii
        $o2 = "s`et" nocase ascii
        $o3 = "iex(" nocase ascii
        $o4 = "invoke-expression" nocase ascii
    condition:
        any of them
}

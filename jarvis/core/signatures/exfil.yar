rule Exfiltration_UploadTransfer
{
    meta:
        description = "Outbound file upload via curl or wget"
        mitre_technique = "T1041"
    strings:
        $tool1  = "curl" nocase ascii
        $tool2  = "wget" nocase ascii
        $flag1  = "-T " ascii
        $flag2  = "--upload-file" nocase ascii
        $flag3  = "--data-binary" nocase ascii
        $flag4  = "-d @" ascii
    condition:
        any of ($tool1, $tool2) and any of ($flag1, $flag2, $flag3, $flag4)
}

rule Exfiltration_Archiving
{
    meta:
        description = "File archiving before exfiltration"
        mitre_technique = "T1560"
    strings:
        $a1 = "compress-archive" nocase ascii
        $a2 = "7z a" nocase ascii
        $a3 = "rar a" nocase ascii
        $a4 = "tar -czf" nocase ascii
        $a5 = "zip -r" nocase ascii
    condition:
        any of them
}

rule Exfiltration_DNSTunneling
{
    meta:
        description = "DNS tunneling or base64-over-DNS indicators"
        mitre_technique = "T1048.003"
    strings:
        $d1 = "nslookup" nocase ascii
        $d2 = "base64" nocase ascii
        $d3 = "resolve-dnsname" nocase ascii
    condition:
        $d1 and ($d2 or $d3)
}

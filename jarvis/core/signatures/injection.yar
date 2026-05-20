rule ProcessInjection_APIIndicators
{
    meta:
        description = "Process injection API call indicators"
        mitre_technique = "T1055"
    strings:
        $i1 = "virtualalloc" nocase ascii
        $i2 = "writeprocessmemory" nocase ascii
        $i3 = "createremotethread" nocase ascii
        $i4 = "ntcreatethread" nocase ascii
        $i5 = "shellcode" nocase ascii
        $i6 = "reflectiveloader" nocase ascii
        $i7 = "loadlibrarya" nocase ascii
    condition:
        any of them
}

rule ProcessInjection_DLL
{
    meta:
        description = "DLL injection via rundll32 or regsvr32"
        mitre_technique = "T1055.001"
    strings:
        $d1 = "rundll32" nocase ascii
        $d2 = "regsvr32" nocase ascii
        $d3 = "/i:http" nocase ascii
        $d4 = "scrobj.dll" nocase ascii
    condition:
        any of ($d1, $d2) and any of ($d3, $d4)
}

rule ThreatFeed_Dynamic_IPs {
  meta:
    source = "abuse.ch"
  strings:
    $ip0 = "162.243.103.246"
    $ip1 = "50.16.16.211"
    $ip2 = "34.204.119.63"
    $ip3 = "178.62.3.223"
    $ip4 = "27.133.154.218"
  condition:
    $ip0 or $ip1 or $ip2 or $ip3 or $ip4
}

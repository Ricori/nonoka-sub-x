package netproxy

import "strings"

// parseSystemProxy turns the registry's ProxyServer value into a proxy URL.
// Windows stores either one "host:port" for every protocol, or a per-protocol
// list such as "http=host:port;https=host:port;socks=host:port". A PAC script
// or ProxyOverride bypass list is not interpreted: the proxy clients users run
// for YouTube set a plain server, and googlevideo is never on a bypass list.
func parseSystemProxy(server string) string {
	server = strings.TrimSpace(server)
	if server == "" {
		return ""
	}
	if !strings.Contains(server, "=") {
		return withProxyScheme(server, "http")
	}
	byProtocol := map[string]string{}
	for _, entry := range strings.Split(server, ";") {
		protocol, address, found := strings.Cut(entry, "=")
		protocol = strings.ToLower(strings.TrimSpace(protocol))
		address = strings.TrimSpace(address)
		if found && address != "" {
			byProtocol[protocol] = address
		}
	}
	for _, protocol := range []string{"https", "http"} {
		if address := byProtocol[protocol]; address != "" {
			return withProxyScheme(address, "http")
		}
	}
	if address := byProtocol["socks"]; address != "" {
		return withProxyScheme(address, "socks5")
	}
	return ""
}

func withProxyScheme(address, scheme string) string {
	if strings.Contains(address, "://") {
		return address
	}
	return scheme + "://" + address
}

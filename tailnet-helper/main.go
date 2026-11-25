// satailnet: a tiny Tailscale node that Server Assistant starts by itself.
// It joins your tailnet as its own device (no separate Tailscale install, no system VPN) and either
//   - serve mode:   accepts connections on the tailnet and passes them to the local dashboard, or
//   - connect mode: opens a local port that passes connections through the tailnet to the server.
//
// Progress is printed as one JSON object per line so Server Assistant can show it.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"tailscale.com/ipn"
	"tailscale.com/net/dnscache"
	"tailscale.com/net/netmon"
	"tailscale.com/tsnet"
)

func emit(v map[string]any) {
	b, _ := json.Marshal(v)
	fmt.Println(string(b))
}

func pipe(a, b net.Conn) {
	defer a.Close()
	defer b.Close()
	done := make(chan struct{}, 2)
	go func() { io.Copy(a, b); done <- struct{}{} }()
	go func() { io.Copy(b, a); done <- struct{}{} }()
	<-done
}

// installInterfaceGetter gives Tailscale the phone's network interfaces from the app (SA_IFACES), because Android
// forbids a plain program from asking the system itself ("netlinkrib: permission denied"). Format, one interface per
// ";" : index|name|mtu|up(1/0)|addr/prefix,addr/prefix. Unset on every other system, which keeps the normal behaviour.
func installInterfaceGetter() {
	spec, ok := os.LookupEnv("SA_IFACES")
	if !ok {
		return
	}
	// On Android, Tailscale only knows which interface carries the default route when the app tells it (the
	// official app does the same through libtailscale); without it no interface is "the" default one.
	if name := os.Getenv("SA_DEFAULT_IF"); name != "" {
		setDefaultRoute(name)
	}
	netmon.RegisterInterfaceGetter(func() ([]netmon.Interface, error) {
		var out []netmon.Interface
		for _, part := range strings.Split(spec, ";") {
			f := strings.Split(part, "|")
			if len(f) < 5 {
				continue
			}
			idx, _ := strconv.Atoi(f[0])
			mtu, _ := strconv.Atoi(f[2])
			flags := net.FlagBroadcast | net.FlagMulticast
			if f[3] == "1" {
				flags |= net.FlagUp | net.FlagRunning
			}
			if f[1] == "lo" {
				flags |= net.FlagLoopback
			}
			addrs := []net.Addr{}
			for _, a := range strings.Split(f[4], ",") {
				ip, n, err := net.ParseCIDR(a)
				if err != nil {
					continue
				}
				addrs = append(addrs, &net.IPNet{IP: ip, Mask: n.Mask})
			}
			out = append(out, netmon.Interface{Interface: &net.Interface{Index: idx, MTU: mtu, Name: f[1], Flags: flags}, AltAddrs: addrs})
		}
		if len(out) == 0 { // never hand back an empty list
			out = append(out, netmon.Interface{
				Interface: &net.Interface{Index: 1, MTU: 65536, Name: "lo", Flags: net.FlagUp | net.FlagLoopback | net.FlagRunning},
				AltAddrs:  []net.Addr{&net.IPNet{IP: net.IPv4(127, 0, 0, 1), Mask: net.CIDRMask(8, 32)}},
			})
		}
		return out, nil
	})
}

// diagnose writes a few short "diag:" lines (phone only) saying whether this program can reach the internet at all,
// can look up names, and can reach Tailscale's sign-in server. The app shows them when a connection gets stuck.
func diagnose() {
	say := func(format string, args ...any) { fmt.Fprintf(os.Stderr, "diag: "+format+"\n", args...) }
	_, err := os.Stat("/etc/resolv.conf")
	say("resolv.conf present=%v", err == nil)
	if c, err := net.DialTimeout("tcp", "1.1.1.1:443", 5*time.Second); err != nil {
		say("internet (tcp 1.1.1.1:443) FAILED: %v", err)
	} else {
		c.Close()
		say("internet (tcp 1.1.1.1:443) ok")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	if ips, err := dnscache.Get().Forward.LookupHost(ctx, "controlplane.tailscale.com"); err != nil {
		say("lookup controlplane FAILED: %v", err)
	} else {
		say("lookup controlplane ok (%d addresses)", len(ips))
	}
	for _, nw := range []string{"tcp4", "tcp6"} {
		nw := nw
		hc := &http.Client{Timeout: 12 * time.Second, Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, addr string) (net.Conn, error) {
				return (&net.Dialer{Timeout: 8 * time.Second}).DialContext(ctx, nw, addr)
			},
			Proxy: nil,
		}}
		resp, err := hc.Get("https://controlplane.tailscale.com/key?v=152")
		if err != nil {
			say("sign-in server over %s FAILED: %v", nw, err)
			continue
		}
		resp.Body.Close()
		say("sign-in server over %s ok (HTTP %d)", nw, resp.StatusCode)
	}
}

func main() {
	mode := flag.String("mode", "serve", "serve | connect")
	host := flag.String("hostname", "serverassistant", "name of this node on the tailnet")
	dir := flag.String("dir", "", "folder for the node's state")
	port := flag.Int("port", 8765, "dashboard port")
	local := flag.String("local", "127.0.0.1:8765", "serve: local dashboard address")
	remote := flag.String("remote", "serverassistant", "connect: name of the server node")
	listen := flag.String("listen", "127.0.0.1:8766", "connect: local address to open")
	exitWithParent := flag.Bool("exit-with-parent", false, "exit when the program that started this helper goes away (its stdin closes)")
	flag.Parse()

	if *exitWithParent {
		go func() {
			io.Copy(io.Discard, os.Stdin) // returns when the parent process is gone
			os.Exit(0)
		}()
	}

	// On Android there is no /etc/resolv.conf: give Go's resolver a public DNS server to fall back on.
	if _, err := os.Stat("/etc/resolv.conf"); err != nil && runtime.GOOS == "linux" {
		net.DefaultResolver = &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, addr string) (net.Conn, error) {
			d := net.Dialer{Timeout: 5 * time.Second}
			return d.DialContext(ctx, "udp", "1.1.1.1:53")
		}}
	}

	installInterfaceGetter()

	// Tailscale's own name lookups use a resolver of their own, which also needs a server to ask when there is no
	// /etc/resolv.conf (Android): without it the sign-in server's name never resolves.
	if _, err := os.Stat("/etc/resolv.conf"); err != nil && runtime.GOOS == "linux" {
		r := dnscache.Get().Forward
		r.PreferGo = true
		r.Dial = func(ctx context.Context, network, address string) (net.Conn, error) {
			d := net.Dialer{Timeout: 5 * time.Second}
			return d.DialContext(ctx, network, "1.1.1.1:53")
		}
	}

	if _, phone := os.LookupEnv("SA_IFACES"); phone {
		go diagnose()
	}

	logf := func(string, ...any) {}
	if _, phone := os.LookupEnv("SA_IFACES"); phone { // on the phone, keep a short log so a stuck start can be explained
		logf = func(format string, args ...any) { fmt.Fprintf(os.Stderr, "log: "+format+"\n", args...) }
	}
	s := &tsnet.Server{Hostname: *host, Dir: *dir, AuthKey: os.Getenv("TS_AUTHKEY"), Logf: logf}
	defer s.Close()
	if err := s.Start(); err != nil {
		emit(map[string]any{"state": "error", "error": err.Error()})
		os.Exit(1)
	}
	lc, err := s.LocalClient()
	if err != nil {
		emit(map[string]any{"state": "error", "error": err.Error()})
		os.Exit(1)
	}
	ctx := context.Background()

	// Report sign-in links and the final address.
	go func() {
		w, err := lc.WatchIPNBus(ctx, ipn.NotifyInitialState)
		if err != nil {
			return
		}
		defer w.Close()
		for {
			n, err := w.Next()
			if err != nil {
				return
			}
			if n.BrowseToURL != nil {
				emit(map[string]any{"state": "login", "url": *n.BrowseToURL})
			}
		}
	}()
	upCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	st, err := s.Up(upCtx)
	cancel()
	if err != nil {
		emit(map[string]any{"state": "error", "error": "Could not join your network: " + err.Error()})
		os.Exit(1)
	}
	ip := ""
	if len(st.TailscaleIPs) > 0 {
		ip = st.TailscaleIPs[0].String()
	}

	if *mode == "serve" {
		ln, err := s.Listen("tcp", fmt.Sprintf(":%d", *port))
		if err != nil {
			emit(map[string]any{"state": "error", "error": err.Error()})
			os.Exit(1)
		}
		emit(map[string]any{"state": "up", "mode": "serve", "ip": ip, "name": *host, "port": *port})
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				l, err := net.Dial("tcp", *local)
				if err != nil {
					c.Close()
					return
				}
				pipe(c, l)
			}(c)
		}
	}

	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		emit(map[string]any{"state": "error", "error": err.Error()})
		os.Exit(1)
	}
	emit(map[string]any{"state": "up", "mode": "connect", "ip": ip, "name": *remote, "listen": *listen})
	for {
		c, err := ln.Accept()
		if err != nil {
			return
		}
		go func(c net.Conn) {
			var r net.Conn
			var err error
			for try := 0; try < 4; try++ { // the first connections after start-up can take a moment
				dctx, cancel := context.WithTimeout(ctx, 8*time.Second)
				r, err = s.Dial(dctx, "tcp", fmt.Sprintf("%s:%d", *remote, *port))
				cancel()
				if err == nil {
					break
				}
				time.Sleep(time.Duration(try+1) * 500 * time.Millisecond)
			}
			if err != nil {
				c.Close()
				return
			}
			pipe(c, r)
		}(c)
	}
}

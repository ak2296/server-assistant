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
	"os"
	"runtime"
	"time"

	"tailscale.com/ipn"
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

func main() {
	mode := flag.String("mode", "serve", "serve | connect")
	host := flag.String("hostname", "serverassistant", "name of this node on the tailnet")
	dir := flag.String("dir", "", "folder for the node's state")
	port := flag.Int("port", 8765, "dashboard port")
	local := flag.String("local", "127.0.0.1:8765", "serve: local dashboard address")
	remote := flag.String("remote", "serverassistant", "connect: name of the server node")
	listen := flag.String("listen", "127.0.0.1:8766", "connect: local address to open")
	flag.Parse()

	// On Android there is no /etc/resolv.conf: give Go's resolver a public DNS server to fall back on.
	if _, err := os.Stat("/etc/resolv.conf"); err != nil && runtime.GOOS != "windows" {
		net.DefaultResolver = &net.Resolver{PreferGo: true, Dial: func(ctx context.Context, network, addr string) (net.Conn, error) {
			d := net.Dialer{Timeout: 5 * time.Second}
			return d.DialContext(ctx, "udp", "1.1.1.1:53")
		}}
	}

	s := &tsnet.Server{Hostname: *host, Dir: *dir, AuthKey: os.Getenv("TS_AUTHKEY"), Logf: func(string, ...any) {}}
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

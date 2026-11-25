//go:build android

package main

import "tailscale.com/net/netmon"

// setDefaultRoute tells Tailscale which interface carries the phone's internet; on Android it can only learn this
// from the app (Tailscale's own Android app does the same).
func setDefaultRoute(name string) { netmon.UpdateLastKnownDefaultRouteInterface(name) }

//go:build !android

package main

// setDefaultRoute only matters on Android.
func setDefaultRoute(string) {}

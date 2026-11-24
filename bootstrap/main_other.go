//go:build !windows

package main

import "fmt"

func main() {
	fmt.Println("xlauncher bootstrap работает только на Windows")
}

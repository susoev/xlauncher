package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func makeZip(t *testing.T, path string, files map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	w := zip.NewWriter(f)
	for name, body := range files {
		fw, _ := w.Create(name)
		fw.Write([]byte(body))
	}
	w.Close()
	f.Close()
}

func TestExtractZipRejectsTraversal(t *testing.T) {
	dir := t.TempDir()
	z := filepath.Join(dir, "bad.zip")
	makeZip(t, z, map[string]string{"../evil.txt": "x"})
	if err := extractZip(z, filepath.Join(dir, "out")); err == nil {
		t.Fatal("ожидалась ошибка для пути с ..")
	}
}

func TestInstallApp(t *testing.T) {
	dir := t.TempDir()
	z := filepath.Join(dir, "app.zip")
	makeZip(t, z, map[string]string{"python/pythonw.exe": "stub", "src/xlauncher/VERSION": "2026.1.1.1"})
	body, _ := os.ReadFile(z)
	sum := sha256.Sum256(body)

	m := Manifest{Version: "2026.1.1.1", AppAsset: "xlauncher-app-2026.1.1.1.zip", AppSHA256: hex.EncodeToString(sum[:])}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest/download/manifest.json":
			json.NewEncoder(w).Encode(m)
		case "/download/v2026.1.1.1/xlauncher-app-2026.1.1.1.zip":
			w.Write(body)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	releaseBase = srv.URL

	got, err := fetchManifest()
	if err != nil || got.Version != m.Version {
		t.Fatalf("fetchManifest: %v %+v", err, got)
	}
	root := filepath.Join(dir, "root")
	if err := installApp(root, got); err != nil {
		t.Fatal(err)
	}
	if readTrim(filepath.Join(root, "app", "2026.1.1.1", "src", "xlauncher", "VERSION")) != "2026.1.1.1" {
		t.Fatal("файлы версии не распакованы")
	}

	bad := *got
	bad.AppSHA256 = "00"
	if err := installApp(filepath.Join(dir, "root2"), &bad); err == nil {
		t.Fatal("ожидалась ошибка SHA-256")
	}
}

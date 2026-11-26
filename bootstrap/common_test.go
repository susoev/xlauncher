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
	"strings"
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

func fakeRoot(t *testing.T) string {
	root := filepath.Join(t.TempDir(), "xl")
	for _, d := range []string{"app/2026.1.1.1/python", "profiles/a/data", "run", "logs", "engine"} {
		os.MkdirAll(filepath.Join(root, d), 0o755)
	}
	for _, f := range []string{"xlauncher.exe", "MOVE.txt", "settings.json", "profiles/a/profile.json", "run/wipe-request.json"} {
		os.WriteFile(filepath.Join(root, f), []byte("x"), 0o644)
	}
	return root
}

func TestWipeKeepsOnlyExeAndMoveTxt(t *testing.T) {
	root := fakeRoot(t)
	left, err := wipeRoot(root, []string{"xlauncher.exe", "MOVE.txt", "settings.json", "../etc"})
	if err != nil || len(left) != 0 {
		t.Fatalf("wipe: %v %v", left, err)
	}
	entries, _ := os.ReadDir(root)
	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	if strings.Join(names, ",") != "MOVE.txt,xlauncher.exe" {
		t.Fatalf("осталось: %v", names)
	}
}

func TestWipeWithoutMoveTxt(t *testing.T) {
	root := fakeRoot(t)
	if _, err := wipeRoot(root, []string{"xlauncher.exe"}); err != nil {
		t.Fatal(err)
	}
	if exists(filepath.Join(root, "MOVE.txt")) || !exists(filepath.Join(root, "xlauncher.exe")) {
		t.Fatal("MOVE.txt должен удалиться, exe — остаться")
	}
}

func TestWipeRefusesDangerousRoots(t *testing.T) {
	home, _ := os.UserHomeDir()
	bare := t.TempDir() // без xlauncher.exe и app
	os.WriteFile(filepath.Join(bare, "important.txt"), []byte("x"), 0o644)
	for _, r := range []string{"/", home, filepath.Dir(home), bare} {
		if _, err := wipeRoot(r, nil); err == nil {
			t.Fatalf("очистка %s должна быть отклонена", r)
		}
	}
	if !exists(filepath.Join(bare, "important.txt")) {
		t.Fatal("тронут файл в чужой папке")
	}
}

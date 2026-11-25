package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Версия загрузчика. Повышать при любом изменении кода загрузчика:
// приложение сравнивает её с манифестом релиза и подменяет xlauncher.exe.
const bootstrapVersion = 4

const repo = "susoev/xlauncher"

var releaseBase = "https://github.com/" + repo + "/releases"

type Manifest struct {
	Version          string `json:"version"`
	AppAsset         string `json:"app_asset"`
	AppSHA256        string `json:"app_sha256"`
	BootstrapVersion int    `json:"bootstrap_version"`
	BootstrapAsset   string `json:"bootstrap_asset"`
	BootstrapSHA256  string `json:"bootstrap_sha256"`
}

var httpClient = &http.Client{Timeout: 10 * time.Minute}

func readTrim(path string) string {
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// writeAtomic пишет файл через временный и переименование.
func writeAtomic(path, content string) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func fetchManifest() (*Manifest, error) {
	resp, err := httpClient.Get(releaseBase + "/latest/download/manifest.json")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("manifest: HTTP %d", resp.StatusCode)
	}
	var m Manifest
	if err := json.NewDecoder(resp.Body).Decode(&m); err != nil {
		return nil, err
	}
	if m.Version == "" || m.AppAsset == "" || m.AppSHA256 == "" {
		return nil, fmt.Errorf("manifest: неполный")
	}
	return &m, nil
}

// download скачивает файл в dst и проверяет SHA-256.
func download(url, dst, wantSHA string) error {
	resp, err := httpClient.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("%s: HTTP %d", url, resp.StatusCode)
	}
	f, err := os.Create(dst)
	if err != nil {
		return err
	}
	h := sha256.New()
	_, err = io.Copy(io.MultiWriter(f, h), resp.Body)
	f.Close()
	if err != nil {
		os.Remove(dst)
		return err
	}
	if got := hex.EncodeToString(h.Sum(nil)); !strings.EqualFold(got, wantSHA) {
		os.Remove(dst)
		return fmt.Errorf("SHA-256 не совпадает: %s", got)
	}
	return nil
}

// extractZip распаковывает архив в dir, не выпуская файлы за его пределы.
func extractZip(zipPath, dir string) error {
	r, err := zip.OpenReader(zipPath)
	if err != nil {
		return err
	}
	defer r.Close()
	base, _ := filepath.Abs(dir)
	for _, f := range r.File {
		target := filepath.Join(base, filepath.FromSlash(f.Name))
		if target != base && !strings.HasPrefix(target, base+string(os.PathSeparator)) {
			return fmt.Errorf("недопустимый путь в архиве: %s", f.Name)
		}
		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		rc, err := f.Open()
		if err != nil {
			return err
		}
		out, err := os.Create(target)
		if err != nil {
			rc.Close()
			return err
		}
		_, err = io.Copy(out, rc)
		rc.Close()
		out.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// installApp скачивает и распаковывает версию приложения в root/app/<version>.
func installApp(root string, m *Manifest) error {
	appDir := filepath.Join(root, "app")
	if err := os.MkdirAll(appDir, 0o755); err != nil {
		return err
	}
	zipPath := filepath.Join(appDir, m.Version+".zip.part")
	url := fmt.Sprintf("%s/download/v%s/%s", releaseBase, m.Version, m.AppAsset)
	if err := download(url, zipPath, m.AppSHA256); err != nil {
		return err
	}
	defer os.Remove(zipPath)
	partial := filepath.Join(appDir, m.Version+".partial")
	os.RemoveAll(partial)
	if err := extractZip(zipPath, partial); err != nil {
		os.RemoveAll(partial)
		return err
	}
	final := filepath.Join(appDir, m.Version)
	os.RemoveAll(final)
	return os.Rename(partial, final)
}

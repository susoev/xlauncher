package main

// Очистка папки лаунчера после переезда (приложение выходит с кодом 76 и оставляет run\wipe-request.json).
// Удаляется всё, кроме самого xlauncher.exe и MOVE.txt (если приложение попросило его оставить).

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const exitWipe = 76

// Что можно оставить после очистки: только эти имена в корне.
var wipeKeepAllowed = map[string]bool{"xlauncher.exe": true, "move.txt": true}

type wipeRequest struct {
	Keep []string `json:"keep"`
}

func wipeRequestPath(root string) string { return filepath.Join(root, "run", "wipe-request.json") }

func readWipeRequest(root string) (*wipeRequest, bool) {
	b, err := os.ReadFile(wipeRequestPath(root))
	if err != nil {
		return nil, false
	}
	var r wipeRequest
	if json.Unmarshal(b, &r) != nil {
		return nil, false
	}
	return &r, true
}

// safeWipeRoot — отказ на корне диска, папке пользователя и её родителях, и на папке, где нет лаунчера.
func safeWipeRoot(root string) error {
	abs, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	abs = filepath.Clean(abs)
	if filepath.Dir(abs) == abs {
		return fmt.Errorf("отказ: %s — корень диска", abs)
	}
	if home, err := os.UserHomeDir(); err == nil {
		home = filepath.Clean(home)
		if strings.EqualFold(abs, home) || strings.HasPrefix(strings.ToLower(home)+string(filepath.Separator), strings.ToLower(abs)+string(filepath.Separator)) {
			return fmt.Errorf("отказ: %s — папка пользователя или выше", abs)
		}
	}
	if !exists(filepath.Join(abs, "xlauncher.exe")) || !exists(filepath.Join(abs, "app")) {
		return fmt.Errorf("отказ: в %s нет xlauncher.exe и app — это не папка лаунчера", abs)
	}
	return nil
}

// wipeRoot удаляет содержимое root, кроме keep; run\ — последним (там запрос на очистку).
// Возвращает то, что удалить не удалось.
func wipeRoot(root string, keep []string) ([]string, error) {
	if err := safeWipeRoot(root); err != nil {
		return nil, err
	}
	keepSet := map[string]bool{}
	for _, k := range keep {
		if wipeKeepAllowed[strings.ToLower(k)] {
			keepSet[strings.ToLower(k)] = true
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, err
	}
	names := []string{}
	for _, e := range entries {
		if !keepSet[strings.ToLower(e.Name())] {
			names = append(names, e.Name())
		}
	}
	sort.SliceStable(names, func(i, j int) bool { return names[j] == "run" && names[i] != "run" })
	var left []string
	for _, n := range names {
		if err := os.RemoveAll(filepath.Join(root, n)); err != nil {
			left = append(left, n)
		}
	}
	return left, nil
}

// wipeWithRetry повторяет очистку до timeout: браузеры и раннеры могут ещё держать файлы.
func wipeWithRetry(root string, keep []string, timeout time.Duration) ([]string, error) {
	deadline := time.Now().Add(timeout)
	for {
		left, err := wipeRoot(root, keep)
		if err != nil || len(left) == 0 || time.Now().After(deadline) {
			return left, err
		}
		time.Sleep(time.Second)
	}
}

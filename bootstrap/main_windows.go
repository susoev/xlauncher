//go:build windows

// xlauncher.exe — загрузчик. Меняется редко.
//
//   - при первом запуске скачивает последнюю версию приложения из GitHub Releases;
//   - запускает версию из app\current.txt и ждёт от неё сигнала готовности (run\ready-<версия>);
//   - не дождался — помечает версию сбойной (app\bad.txt) и откатывается на app\previous.txt;
//   - код выхода 75 — приложение обновилось и просит перезапуск;
//   - работает в одном экземпляре; повторный запуск просит открытое окно показаться.
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"
)

const (
	exitRestart      = 75
	readyTimeout     = 60 * time.Second
	crashWindow      = 10 * time.Minute
	crashLimit       = 3
	errAlreadyExists = 183
)

var minimized bool

var (
	kernel32        = syscall.NewLazyDLL("kernel32.dll")
	user32          = syscall.NewLazyDLL("user32.dll")
	procCreateMutex = kernel32.NewProc("CreateMutexW")
	procMessageBox  = user32.NewProc("MessageBoxW")
)

func messageBox(text string) {
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString("xlauncher")
	procMessageBox.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), 0x10) // MB_ICONERROR
}

func fail(format string, a ...any) {
	msg := fmt.Sprintf(format, a...)
	log.Print("ОШИБКА: " + msg)
	messageBox(msg)
	os.Exit(1)
}

func main() {
	exe, err := os.Executable()
	if err != nil {
		messageBox("Не удалось определить путь к xlauncher.exe")
		os.Exit(1)
	}
	root := filepath.Dir(exe)
	for _, d := range []string{"app", "run", "logs"} {
		os.MkdirAll(filepath.Join(root, d), 0o755)
	}
	lf, err := os.OpenFile(filepath.Join(root, "logs", "bootstrap.log"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err == nil {
		log.SetOutput(lf)
		defer lf.Close()
	}
	log.SetFlags(log.LstdFlags)

	// MCP-сервер для Claude и других агентов: stdio передаётся текущей версии приложения.
	// Работает параллельно с окном лаунчера, поэтому до проверки «один экземпляр».
	if len(os.Args) > 1 && os.Args[1] == "--mcp" {
		os.Exit(runMCP(root))
	}

	for _, a := range os.Args[1:] {
		if a == "--minimized" {
			minimized = true
		}
	}

	// Один экземпляр на пользователя Windows.
	name, _ := syscall.UTF16PtrFromString(`Local\xlauncher-bootstrap`)
	h, _, callErr := procCreateMutex.Call(0, 0, uintptr(unsafe.Pointer(name)))
	if h != 0 && callErr == syscall.Errno(errAlreadyExists) {
		os.WriteFile(filepath.Join(root, "run", "show-request"), []byte(time.Now().String()), 0o644)
		return
	}
	releaseMutex := func() {
		if h != 0 {
			syscall.CloseHandle(syscall.Handle(h))
			h = 0
		}
	}
	defer releaseMutex()

	log.Printf("загрузчик v%d, корень %s", bootstrapVersion, root)
	info, _ := json.Marshal(map[string]any{"version": bootstrapVersion, "pid": os.Getpid()})
	os.WriteFile(filepath.Join(root, "run", "bootstrap.json"), info, 0o644)
	os.Remove(filepath.Join(root, "xlauncher.old.exe")) // остался от прошлой подмены загрузчика

	appDir := filepath.Join(root, "app")
	current := filepath.Join(appDir, "current.txt")
	previous := filepath.Join(appDir, "previous.txt")

	if ver := readTrim(current); ver == "" || !exists(filepath.Join(appDir, ver)) {
		log.Print("первый запуск: скачиваю последнюю версию")
		m, err := fetchManifest()
		if err != nil {
			fail("Не удалось получить сведения о последней версии: %v\n\nПроверь интернет и запусти xlauncher.exe ещё раз.", err)
		}
		if err := installApp(root, m); err != nil {
			fail("Не удалось скачать xlauncher %s: %v", m.Version, err)
		}
		if err := writeAtomic(current, m.Version); err != nil {
			fail("Не удалось записать %s: %v", current, err)
		}
		log.Printf("установлена версия %s", m.Version)
	}

	var crashes []time.Time
	for {
		ver := readTrim(current)
		code, ready := launch(root, ver)
		log.Printf("версия %s завершилась: код %d, готовность %v", ver, code, ready)

		switch {
		case !ready:
			appendLine(filepath.Join(appDir, "bad.txt"), ver)
			prev := readTrim(previous)
			if prev != "" && prev != ver && exists(filepath.Join(appDir, prev)) {
				log.Printf("откат %s → %s", ver, prev)
				writeAtomic(current, prev)
				writeAtomic(previous, "")
				continue
			}
			fail("xlauncher %s не запустился, и откатываться не на что.\nЖурналы: %s", ver, filepath.Join(root, "logs"))

		case code == exitRestart:
			marker := filepath.Join(root, "run", "bootstrap-replaced")
			if exists(marker) {
				os.Remove(marker)
				log.Print("загрузчик обновлён — перезапускаю себя")
				releaseMutex()
				if err := exec.Command(filepath.Join(root, "xlauncher.exe")).Start(); err != nil {
					log.Printf("не удалось запустить новый загрузчик: %v", err)
				}
				return
			}
			continue

		case code == 0:
			return

		default:
			now := time.Now()
			crashes = append(crashes, now)
			recent := crashes[:0]
			for _, t := range crashes {
				if now.Sub(t) < crashWindow {
					recent = append(recent, t)
				}
			}
			crashes = recent
			if len(crashes) >= crashLimit {
				fail("xlauncher %s аварийно завершился %d раза за 10 минут.\nЖурналы: %s", ver, len(crashes), filepath.Join(root, "logs"))
			}
			time.Sleep(2 * time.Second)
		}
	}
}

// launch запускает версию и возвращает код выхода и то, успела ли она сообщить о готовности.
func launch(root, ver string) (int, bool) {
	dir := filepath.Join(root, "app", ver)
	readyFile := filepath.Join(root, "run", "ready-"+ver)
	os.Remove(readyFile)

	exe := filepath.Join(dir, "python", "XLauncher.exe") // своё имя в диспетчере задач
	if !exists(exe) {
		exe = filepath.Join(dir, "python", "pythonw.exe") // версии до 2026.11.24.7
	}
	cmd := exec.Command(exe, "-m", "xlauncher")
	cmd.Dir = dir
	cmd.Env = append(os.Environ(),
		"XL_ROOT="+root,
		fmt.Sprintf("XL_BOOTSTRAP_VERSION=%d", bootstrapVersion),
	)
	if minimized {
		cmd.Env = append(cmd.Env, "XL_MINIMIZED=1") // автозапуск с Windows: окно свёрнуто
		minimized = false                           // только для первого запуска, не после обновлений
	}
	if err := cmd.Start(); err != nil {
		log.Printf("не удалось запустить %s: %v", ver, err)
		return -1, false
	}
	done := make(chan struct{})
	go func() { cmd.Wait(); close(done) }()

	deadline := time.After(readyTimeout)
	tick := time.NewTicker(300 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-done:
			return cmd.ProcessState.ExitCode(), exists(readyFile)
		case <-deadline:
			if exists(readyFile) {
				<-done
				return cmd.ProcessState.ExitCode(), true
			}
			log.Printf("версия %s не сообщила о готовности за %s", ver, readyTimeout)
			cmd.Process.Kill()
			<-done
			return -1, false
		case <-tick.C:
			if exists(readyFile) {
				<-done
				return cmd.ProcessState.ExitCode(), true
			}
		}
	}
}

func appendLine(path, line string) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString(line + "\n")
}

// runMCP запускает python -m xlauncher.mcp_server текущей версии с унаследованными stdin/stdout.
func runMCP(root string) int {
	ver := readTrim(filepath.Join(root, "app", "current.txt"))
	py := filepath.Join(root, "app", ver, "python", "python.exe")
	if ver == "" || !exists(py) {
		fmt.Fprintln(os.Stderr, "XLauncher ещё не установлен: запусти xlauncher.exe один раз без параметров")
		return 1
	}
	cmd := exec.Command(py, "-m", "xlauncher.mcp_server")
	cmd.Dir = filepath.Join(root, "app", ver)
	cmd.Env = append(os.Environ(), "XL_ROOT="+root, "PYTHONIOENCODING=utf-8")
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	log.Printf("MCP: версия %s", ver)
	if err := cmd.Run(); err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			return ee.ExitCode()
		}
		log.Printf("MCP: %v", err)
		return 1
	}
	return 0
}

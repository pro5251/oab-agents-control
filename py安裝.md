# Python／`py` 安裝與測試指引

`py` 是 Windows 的 Python Launcher；WSL 是 Linux 環境，通常使用
`python3`，而不是 `py`。請依實際執行指令的終端機選擇下列步驟。

## Windows PowerShell

1. 從 [Python 官方下載頁](https://www.python.org/downloads/windows/) 安裝 Python。
2. 安裝畫面勾選 **Add python.exe to PATH** 與 **Install launcher for all users**
   （或 **Install Python launcher**）。
3. 關閉並重新開啟 PowerShell，確認安裝：

   ```powershell
   py --version
   py -m pip --version
   ```

4. 在專案根目錄執行測試：

   ```powershell
   $env:PYTHONPATH = "."
   py -m unittest discover -s tests -v
   ```

`PYTHONPATH=. command` 是 Bash 語法，不能直接在 PowerShell 使用。

## WSL（Ubuntu／Debian）

在 WSL 終端機安裝 Python：

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
```

確認安裝：

```bash
python3 --version
python3 -m pip --version
```

在專案根目錄執行測試：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

若希望在 WSL 中以 `python` 代替 `python3`，可額外安裝：

```bash
sudo apt install -y python-is-python3
```

之後可使用：

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
```

## 常見問題

- PowerShell 顯示找不到 `PYTHONPATH=.`：請改用 `$env:PYTHONPATH = "."`，再執行命令。
- WSL 顯示找不到 `py`：這是正常情況，請使用 `python3`。
- 終端機找不到 Python：安裝後重新開啟終端機；Windows 可先執行 `py --version`，WSL 可先執行 `python3 --version`。

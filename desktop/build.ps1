# 桌面包构建（PowerShell）
# 前置：.venv 激活 + frontend/dist 已构建
#   pip install -r requirements.txt
#   cd frontend && npm ci && npm run build && cd ..
#   pip install pyinstaller
pyinstaller desktop/finagent.spec --noconfirm --workpath desktop/build --distpath desktop/dist
Write-Host "产物: desktop/dist/finagent/finagent.exe"

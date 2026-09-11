@echo off
rem ===========================================================================
rem  E2E verification with a TEMPORARY MySQL instance (no admin rights needed)
rem  - creates a throwaway datadir under %TEMP%\bbs_mysql_e2e
rem  - listens on port 3307, so your real MySQL (3306) is never touched
rem  - imports sql\bbs_schema.sql, prints table/seed counts, runs tools\smoke_test.py
rem  - shuts the temp instance down and deletes the datadir at the end
rem  Usage:  cmd /c tools\e2e_with_temp_mysql.bat     (log: _e2e.txt)
rem ===========================================================================
setlocal
set "ROOT=%~dp0..\"
set "MYSQLD=D:\Mysql\mysql-8.0.46-winx64\bin\mysqld.exe"
set "MYSQL=D:\Mysql\mysql-8.0.46-winx64\bin\mysql.exe"
set "DATA=%TEMP%\bbs_mysql_e2e"
set "PORT=3307"
set "LOG=%ROOT%_e2e.txt"

echo ==== 1. initialize temp datadir ==== > "%LOG%"
if not exist "%DATA%\auto.cnf" (
  rmdir /s /q "%DATA%" 2>nul
  "%MYSQLD%" --initialize-insecure --datadir="%DATA%" --console >> "%LOG%" 2>&1
)
echo init-finished >> "%LOG%"

echo ==== 2. start mysqld on port %PORT% ==== >> "%LOG%"
start "bbs-e2e-mysql" /min "%MYSQLD%" --datadir="%DATA%" --port=%PORT% --console
for /l %%i in (1,1,60) do (
  "%MYSQL%" -h 127.0.0.1 -P %PORT% -u root -e "select version()" >nul 2>&1 && goto ready
  ping -n 2 127.0.0.1 >nul
)
:ready
echo mysqld-ready >> "%LOG%"

echo ==== 3. import sql/bbs_schema.sql ==== >> "%LOG%"
"%MYSQL%" -h 127.0.0.1 -P %PORT% -u root --default-character-set=utf8mb4 < "%ROOT%sql\bbs_schema.sql" >> "%LOG%" 2>&1
echo import-exit=%ERRORLEVEL% >> "%LOG%"

echo ==== 4. verify tables + seed ==== >> "%LOG%"
"%MYSQL%" -h 127.0.0.1 -P %PORT% -u root -e "use bbs_app; show tables; select (select count(*) from users) users,(select count(*) from bars) bars,(select count(*) from posts) posts,(select count(*) from post_images) images,(select count(*) from comments) comments,(select count(*) from likes) likes,(select count(*) from favorites) favs,(select count(*) from follows) follows;" >> "%LOG%" 2>&1
echo verify-exit=%ERRORLEVEL% >> "%LOG%"

echo ==== 5. smoke test against the temp instance ==== >> "%LOG%"
set "DB_HOST=127.0.0.1"
set "DB_PORT=%PORT%"
set "DB_USER=root"
set "DB_PASSWORD="
set "DB_NAME=bbs_app"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%ROOT%.venv\Scripts\python.exe" "%ROOT%tools\smoke_test.py" >> "%LOG%" 2>&1
echo smoke-exit=%ERRORLEVEL% >> "%LOG%"

echo ==== 6. shutdown temp instance + cleanup ==== >> "%LOG%"
"%MYSQL%" -h 127.0.0.1 -P %PORT% -u root -e "shutdown" >> "%LOG%" 2>&1
ping -n 4 127.0.0.1 >nul
rmdir /s /q "%DATA%" 2>nul
echo cleanup-done >> "%LOG%"

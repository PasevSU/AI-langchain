# Проверки преди издание

Тези три команди се пускат от корена на хранилището и трябва да минат, преди версията да бъде обявена за издадена. `make` не се използва — той не е наличен на всяка машина.

```powershell
uv run --no-project ruff check installer
uv run --no-project mypy installer/pasevsu_installer
python -m unittest discover -s installer/tests -t installer
```

Очакван резултат: `All checks passed!`, `Success: no issues found`, `OK`.

Пакетът `langchain-v1-unit` е незадължителен: част от тестовете му са само за POSIX и се провалят на Windows. Провалът му не блокира публикуването, но трябва да бъде отчетен в бележките към изданието.

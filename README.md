Для запуска на ПК нужно зайти в папку со скаченным репозиторием и вставить команду ".\.venv\Scripts\python.exe -X utf8 server.py --open"
Для обновления Репозитория на Пк нужно один раз самый первый подключить обновление "git remote add upstream https://github.com/Ogyod/Algoritmi.git"
После этого, загрузи новую версию Репозитория в эту же папку "git pull --ff-only upstream main"
Обнови зависимости и запусти приложение ".\.venv\Scripts\python.exe -m pip install -r requirements.txt" и "npm.cmd install --ignore-scripts" Запуск осуществляется командой, указанной в начале


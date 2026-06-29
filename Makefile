run:
	python manage.py runserver 0.0.0.0:8000

migrate:
	python manage.py migrate

makemigrations:
	python manage.py makemigrations

createsuperuser:
	python manage.py createsuperuser

test:
	pytest

lint:
	ruff check .

format:
	ruff format .

celery:
	celery -A config worker -l info

beat:
	celery -A config beat -l info

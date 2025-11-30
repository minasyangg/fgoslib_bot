"""
Пример вызова HF Space через gradio_client.Client.

Использование:
  HF_SPACE=mingg93/fgoslib-qwen3 HF_API_TOKEN=<token> python tests/gradio_client_example.py
или
  python tests/gradio_client_example.py --space mingg93/fgoslib-qwen3 --token <token>

Сценарии:
 - показывает, как передать `text_input` и `image_input` (через `handle_file`)
 - обрабатывает `gradio_client.exceptions.AppError` и показывает, как детектировать ZeroGPU/квоту
 - печатает структуру ответа для удобной отладки
"""
import os
import sys
import argparse
import traceback

try:
    from gradio_client import Client, handle_file
    from gradio_client.client import AppError as GradioAppError
except Exception as e:
    print('Ошибка импорта gradio_client:', e)
    print('Установите пакет: pip install gradio_client')
    raise


def call_space(space: str, token: str | None, text_input: str, image_path: str | None, api_name: str = '/solve_problem'):
    # Создаём клиент; если token передан, используем его
    client = None
    try:
        if token:
            client = Client(space, hf_token=token)
        else:
            client = Client(space)
    except Exception:
        print('Не удалось создать gradio_client.Client для', space)
        traceback.print_exc()
        return

    # Подготовка image_input: если есть локальный путь, оборачиваем его в handle_file
    image_input = None
    if image_path:
        if not os.path.exists(image_path):
            print('Файл изображения не найден:', image_path)
            return
        image_input = handle_file(image_path)

    print('Отправляю запрос в Space...')
    try:
        res = client.predict(
            text_input=text_input,
            image_input=image_input,
            api_name=api_name
        )
        print('\n=== Успешный ответ от Space ===')
        print('type(res)=', type(res))
        # Печать короткого превью результата
        if isinstance(res, (list, tuple)):
            print('len:', len(res))
            for i, el in enumerate(res):
                print(f'  [{i}] type={type(el)}')
                try:
                    if isinstance(el, str):
                        print('    preview str:', el[:400])
                    elif isinstance(el, bytes):
                        print('    bytes len:', len(el))
                    else:
                        print('    repr:', repr(el)[:400])
                except Exception:
                    pass
        elif isinstance(res, dict):
            print('keys:', list(res.keys()))
            for k, v in res.items():
                print(f'  {k}: type={type(v)}')
        else:
            print('repr:', repr(res)[:1000])

    except GradioAppError as ae:
        msg = str(ae)
        print('\nGradio AppError:', msg)
        low = msg.lower()
        if any(x in low for x in ('no gpu', 'no gpu was available', 'create a free account', 'zerogpu', 'quota')):
            print('\nПохоже на ZeroGPU/quota ошибку со стороны HF.')
        else:
            print('\nAppError (неясная причина)')
        traceback.print_exc()
    except Exception as e:
        print('\nОшибка при вызове client.predict:')
        traceback.print_exc()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--space', '-s', default=os.environ.get('HF_SPACE'))
    p.add_argument('--token', '-t', default=os.environ.get('HF_API_TOKEN'))
    p.add_argument('--image', '-i', default=None)
    p.add_argument('--api-name', '-a', default=os.environ.get('HF_API_NAME', '/solve_problem'))
    args = p.parse_args()

    if not args.space:
        print('Укажите Space через --space или переменную окружения HF_SPACE')
        sys.exit(2)

    sample_text = 'Hello!! Это тестовый запрос для проверки подключения к Space.'

    call_space(args.space, args.token, sample_text, args.image, api_name=args.api_name)

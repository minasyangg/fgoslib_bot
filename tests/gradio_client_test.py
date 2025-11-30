"""
Тестовый скрипт для проверки подключения к Hugging Face Space через gradio_client.
Запуск:

HF_SPACE и при необходимости HF_TOKEN должны быть в окружении, или переданы через аргументы.

Пример:
HF_SPACE=mingg93/fgoslib-qwen3 python tests/gradio_client_test.py

Этот скрипт выводит подробную информацию о ответе или ошибках (в т.ч. AppError с сообщением о квоте No GPU).
"""
import os
import sys
import argparse
import traceback

try:
    from gradio_client import Client
    from gradio_client.client import AppError as GradioAppError
except Exception as e:
    print("gradio_client не установлен или не импортируется:", e)
    print("Установите пакет: pip install gradio_client")
    raise

from prompt_example import get_sample_problem


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--space', '-s', default=os.environ.get('HF_SPACE'), help='HF Space id (owner/repo)')
    p.add_argument('--api-name', '-a', default=os.environ.get('HF_API_NAME', '/solve_problem'))
    p.add_argument('--token', '-t', default=os.environ.get('HF_TOKEN'))
    args = p.parse_args()

    if not args.space:
        print('Не указан HF Space. Задайте через --space или HF_SPACE в окружении.')
        sys.exit(2)

    print('Space:', args.space)
    print('API name:', args.api_name)

    client = None
    try:
        if args.token:
            client = Client(args.space, hf_token=args.token)
        else:
            client = Client(args.space)
    except Exception:
        print('Не удалось создать Client для Space:', args.space)
        traceback.print_exc()
        sys.exit(1)

    text_input = get_sample_problem()
    image_input = None

    print('\n=== Отправляем тестовый запрос ===')
    try:
        # Вызов predict — возможны AppError, в том числе про ZeroGPU
        res = client.predict(
            text_input=text_input,
            image_input=image_input,
            api_name=args.api_name
        )
        print('Успешный ответ от Space:')
        print('Тип ответа:', type(res))
        # Несколько полезных снимков
        try:
            if isinstance(res, (list, tuple)):
                print('len:', len(res))
                for i,el in enumerate(res):
                    t = type(el)
                    print(f'  [{i}] type={t}')
                    if isinstance(el, (str, bytes)):
                        s = el
                        if isinstance(el, bytes):
                            s = el[:200]
                        print('    preview:', str(s)[:400])
                    elif el is None:
                        print('    None')
                    else:
                        # dict or file-like
                        try:
                            print('    repr:', repr(el)[:500])
                        except Exception:
                            pass
            elif isinstance(res, dict):
                print('keys:', list(res.keys()))
            else:
                print('repr:', repr(res)[:1000])
        except Exception:
            traceback.print_exc()

    except GradioAppError as ae:
        msg = str(ae)
        print('\nGradio AppError:', msg)
        low = msg.lower()
        if 'no gpu' in low or 'no gpu was available' in low or 'create a free account' in low or 'zerogpu' in low:
            print('\nПохоже, проблема на стороне HF (ZeroGPU / квота).')
        else:
            print('\nAppError выглядит иначе — проверьте сообщение полностью.')
        traceback.print_exc()
    except Exception as e:
        print('\nОшибка при вызове predict:')
        traceback.print_exc()


if __name__ == '__main__':
    main()

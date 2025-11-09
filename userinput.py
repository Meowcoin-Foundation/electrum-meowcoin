# userinput.py
import pyperclip
user_input = input("prompt: ")
if user_input.strip() == "/paste":
    user_input = pyperclip.paste()
    print(f"[Clipboard]: {user_input}") 
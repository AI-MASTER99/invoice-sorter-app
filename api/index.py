import sys
import os

# Voeg de root toe zodat Python main.py kan vinden
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from invoiceflow.main import app

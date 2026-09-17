# Contributing

Contributions are welcome. Please keep the controller generic: study-specific analysis belongs inside an individual dashboard project, not in `dashboard_controller.py`.

Before opening a pull request:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Do not include credentials, real study data, generated `Outputs/`, Quarto render directories or local runtime configuration.

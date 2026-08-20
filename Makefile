.PHONY: demo test eval clean ui
demo:
	python3 data/make_claims.py
	python3 pipeline.py --all
test:
	python3 -m pytest -q
eval:
	python3 p01_structured_output/main.py --all
	python3 p07_cost_router/main.py --compare
	python3 p07_cost_router/main.py --mix
	python3 p11_observability/main.py --dashboard
	python3 p11_observability/main.py --alerts
ui:
	streamlit run app/streamlit_app.py
clean:
	rm -f data/agent.db data/tmp.db
	find . -name __pycache__ -type d -exec rm -rf {} +

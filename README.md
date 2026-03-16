
# Required setup
Need these two files 
- pyproject.toml
- wrapper.py
'''
npm install -g wrangler@latest
wranger dev

pip install uv
uv tool install workers-py
uv run pywrangler init


'''

# Start the app in local
```
wrangler dev --port 8788
```

# Deploy to Cloudflare Pages
Keep all variables as secret type
```
> Deploy command:npx wrangler deploy --keep-vars
> Put Non-production branch deployment command as : npx wrangler versions upload
```
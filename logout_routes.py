# Logout routes for AssistX

# Add these routes to server.py after the dashboard routes


@app.get("/admin/logout")
async def admin_logout(request: Request):
    """Admin logout - clear session and redirect to admin login"""
    request.session.clear()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/user/logout")
async def user_logout(request: Request):
    """User logout - clear session and redirect to user login"""
    request.session.clear()
    return RedirectResponse(url="/user/login", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    """Generic logout endpoint - clear session and redirect to home"""
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)

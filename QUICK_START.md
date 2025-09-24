# 🚀 CodeFlow Quick Start Guide

## Option 1: Demo Mode (Instant Start)
**No setup required!** Just click the "Try Demo Mode" button on the login screen to test all features immediately.

## Option 2: Full Setup with Supabase (5 minutes)

### 1. Create Supabase Project
1. Go to [supabase.com](https://supabase.com)
2. Click "Start your project"
3. Create a new organization if needed
4. Create a new project (choose any name)
5. Wait for project setup (1-2 minutes)

### 2. Get Your Credentials
1. In your Supabase dashboard, go to **Settings** → **API**
2. Copy these values:
   - **Project URL** (starts with https://)
   - **anon public key** (starts with eyJ...)
   - **service_role key** (starts with eyJ...)

### 3. Configure CodeFlow
1. Open `.env.local` file in your CodeFlow directory
2. Replace the placeholder values:
```bash
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_ANON_KEY=eyJ...your-anon-key
SUPABASE_SERVICE_ROLE_KEY=eyJ...your-service-role-key
```

### 4. Setup Database
1. In Supabase dashboard, go to **SQL Editor**
2. Copy the contents of `supabase-reset-complete.sql`
3. Paste and click "Run"
4. You should see: "CodeFlow database reset completed successfully!"

### 5. Start Using CodeFlow
1. Restart your server: `npm run dev`
2. Go to http://localhost:5000
3. Create an account and start analyzing code!

## Troubleshooting

### Authentication Errors (401)
- Check your Supabase credentials in `.env.local`
- Make sure you're using the **service_role** key, not anon key
- Verify your Supabase project is active

### MutationObserver Errors
- These are harmless browser extension conflicts
- Already suppressed automatically
- Won't affect CodeFlow functionality

### Server Won't Start
- Run `npm install` to install dependencies
- Check if port 5000 is already in use
- Use `npm start` instead of `npm run dev` if needed

## Features Available
✅ **Single File Analysis** - Upload/paste code for instant analysis  
✅ **Project Mapping** - Multi-file dependency visualization  
✅ **Visual GUI Mapping Engine** - Interactive D3.js graphs  
✅ **Advanced Filtering** - Filter by functions, classes, files  
✅ **Impact Analysis** - Hover to see connected components  
✅ **Real-time Metrics** - Complexity, structure, dependencies  

Need help? Check the full documentation in the repository!
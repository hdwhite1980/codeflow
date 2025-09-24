# 🚀 CodeFlow Production Setup Guide

## Prerequisites
- Node.js installed
- Supabase account (free tier available)
- Git repository access

## Production Setup (5 minutes)

### 1. Create Supabase Project
1. Go to [supabase.com](https://supabase.com) and sign in
2. Click "New project"
3. Choose your organization
4. Create a new project with your preferred name
5. Wait for project initialization (1-2 minutes)

### 2. Get Your Production Credentials
1. In your Supabase dashboard, go to **Settings** → **API**
2. Copy these values:
   - **Project URL** (starts with https://)
   - **anon public key** (starts with eyJ...)
   - **service_role key** (starts with eyJ...)

### 3. Configure Environment Variables
1. Open `.env.local` file in your CodeFlow directory
2. Replace the placeholder values with your actual credentials:
```bash
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_ANON_KEY=eyJ...your-anon-key
SUPABASE_SERVICE_ROLE_KEY=eyJ...your-service-role-key
```

### 4. Initialize Database Schema
1. In Supabase dashboard, go to **SQL Editor**
2. Copy the contents of `supabase-reset-complete.sql`
3. Paste and click "Run"
4. Verify success: "CodeFlow database reset completed successfully!"

### 5. Deploy to Production
1. Restart your server: `npm run dev`
2. Test authentication at http://localhost:5000
3. Create your first account
4. Start analyzing code with the Visual GUI Mapping Engine

## Production Deployment

### Vercel Deployment
```bash
# Install Vercel CLI
npm i -g vercel

# Deploy
vercel --prod

# Set environment variables in Vercel dashboard
```

### Environment Variables for Vercel
Add these in your Vercel dashboard under Settings → Environment Variables:
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY` 
- `SUPABASE_SERVICE_ROLE_KEY`
- `NODE_ENV=production`

## Troubleshooting

### Authentication Errors (401)
- Verify Supabase credentials in `.env.local`
- Ensure you're using the **service_role** key for backend
- Check that your Supabase project is active and properly configured

### Database Connection Issues
- Run the database setup script in Supabase SQL Editor
- Verify all tables and policies were created successfully
- Check Row Level Security (RLS) is enabled

### Server Won't Start
- Run `npm install` to install all dependencies
- Verify port 5000 isn't already in use
- Check `.env.local` file exists and has proper format

### Network/CORS Errors
- Verify API endpoints are accessible
- Check Supabase project settings for CORS configuration
- Ensure proper headers are being sent

## Production Features
✅ **Real Authentication** - Secure user registration and login  
✅ **Database Persistence** - All data stored in Supabase  
✅ **Single File Analysis** - Upload/paste code for instant analysis  
✅ **Project Mapping** - Multi-file dependency visualization  
✅ **Visual GUI Mapping Engine** - Interactive D3.js graphs  
✅ **Advanced Filtering** - Filter by functions, classes, files  
✅ **Impact Analysis** - Hover to see connected components  
✅ **Real-time Metrics** - Complexity, structure, dependencies  
✅ **Production Ready** - Optimized for deployment

## Security Best Practices
- Use environment variables for all credentials
- Never commit API keys to version control
- Enable Row Level Security in Supabase
- Use HTTPS in production
- Regularly update dependencies

For technical support, refer to the complete documentation in the repository.
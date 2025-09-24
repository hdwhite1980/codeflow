# CodeFlow Supabase Setup Guide

This guide will help you set up Supabase authentication for your CodeFlow application.

## Step 1: Create a Supabase Project

1. Go to [supabase.com](https://supabase.com) and sign up/login
2. Click "New Project"
3. Choose your organization
4. Fill in project details:
   - Name: `codeflow` (or your preferred name)
   - Database Password: Generate a secure password
   - Region: Choose closest to your users
5. Click "Create new project"
6. Wait for the project to be set up (takes a few minutes)

## Step 2: Get Your Project Credentials

1. In your Supabase dashboard, go to **Settings > API**
2. You'll need these values:
   - **Project URL** (starts with `https://`)
   - **anon public key** (starts with `eyJ...`)
   - **service_role secret** (starts with `eyJ...`) - Click "Reveal" to see it

## Step 3: Configure Your Environment

1. Copy your current `.env.local` or create it from `.env.example`
2. Replace the placeholder values:

```bash
# Replace with your actual Supabase values
SUPABASE_URL=https://your-actual-project-id.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Keep these for future features
OPENAI_API_KEY=your_openai_api_key_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
GOOGLE_GENERATIVE_AI_API_KEY=your_google_ai_api_key_here

NODE_ENV=production
```

## Step 4: Set Up Database Tables

1. In your Supabase dashboard, go to **SQL Editor**
2. Copy the contents of `supabase-setup.sql` from this project
3. Paste it into the SQL editor and click **Run**
4. This will create:
   - `profiles` table for user profiles
   - `projects` table for user projects (future use)
   - `analysis_history` table for analysis results (future use)
   - Row Level Security policies
   - Auto-profile creation trigger

## Step 5: Configure Vercel Environment Variables

1. Go to your [Vercel Dashboard](https://vercel.com/dashboard)
2. Select your CodeFlow project
3. Go to **Settings > Environment Variables**
4. Add these variables (same values as your `.env.local`):
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `NODE_ENV` = `production`

## Step 6: Test Authentication

1. Deploy your changes: `vercel --prod`
2. Visit your deployed app
3. Try registering a new account
4. Try logging in with the account
5. Check your Supabase dashboard **Authentication > Users** to see registered users

## Step 7: Configure Email Settings (Optional)

For production, you'll want to set up email authentication:

1. In Supabase dashboard, go to **Authentication > Settings**
2. Configure SMTP settings or use Supabase's built-in email
3. Set up custom email templates
4. Configure redirect URLs for your domain

## Troubleshooting

### Common Issues:

1. **"Missing Supabase configuration" error**
   - Check that all environment variables are set correctly
   - Make sure there are no extra spaces in your `.env.local` file

2. **Authentication fails**
   - Verify your Supabase URL is correct (should start with https://)
   - Check that you're using the service role key, not the anon key, in your API

3. **Database errors**
   - Make sure you ran the SQL setup script
   - Check RLS policies are enabled
   - Verify tables were created in **Database > Tables**

4. **Vercel deployment issues**
   - Ensure environment variables are set in Vercel dashboard
   - Check deployment logs for specific errors
   - Make sure function timeout is sufficient

### Useful Commands:

```bash
# Test locally
npm run dev

# Deploy to Vercel
vercel --prod

# Check environment variables
vercel env ls
```

## Security Notes

- Never commit your `.env.local` file to Git
- Use the service role key only in server-side API functions
- The anon key can be used in frontend code (it's designed to be public)
- Row Level Security is enabled to protect user data

## Next Steps

Once authentication is working:
- Users can register and login with real accounts
- Analysis results can be saved to user profiles (future feature)
- Project management can be added (future feature)
- User-specific analysis history (future feature)
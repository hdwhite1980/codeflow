-- Step 1: Create tables only (no policies)
-- Run this first to create the basic structure

-- Create profiles table (extends Supabase auth.users)
CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT,
  name TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (id)
);

-- Projects table (enhanced from existing schema)
CREATE TABLE IF NOT EXISTS public.projects (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL,
  description TEXT,
  repository_url VARCHAR(500),
  files JSONB DEFAULT '[]'::jsonb,
  analysis_results JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Code files table (from existing schema)
CREATE TABLE IF NOT EXISTS public.code_files (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  project_id UUID REFERENCES public.projects(id) ON DELETE CASCADE,
  filename VARCHAR(500) NOT NULL,
  content TEXT,
  language VARCHAR(100),
  file_path VARCHAR(1000),
  file_size INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- AI sessions table (from existing schema)
CREATE TABLE IF NOT EXISTS public.ai_sessions (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  project_id UUID REFERENCES public.projects(id) ON DELETE CASCADE,
  provider VARCHAR(50) NOT NULL, -- 'openai', 'anthropic', 'google'
  model VARCHAR(100) NOT NULL,
  messages JSONB NOT NULL DEFAULT '[]',
  tokens_used INTEGER DEFAULT 0,
  cost DECIMAL(10, 4) DEFAULT 0.0000,
  status VARCHAR(50) DEFAULT 'active', -- 'active', 'completed', 'error'
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Analysis results table (enhanced from existing schema)
CREATE TABLE IF NOT EXISTS public.analysis_results (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  project_id UUID REFERENCES public.projects(id) ON DELETE CASCADE,
  file_id UUID REFERENCES public.code_files(id) ON DELETE CASCADE,
  analysis_type VARCHAR(100) NOT NULL, -- 'complexity', 'security', 'dependencies', 'single-file', 'project-map'
  results JSONB NOT NULL,
  score DECIMAL(5, 2),
  input_data JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Security vulnerabilities table (from existing schema)
CREATE TABLE IF NOT EXISTS public.security_vulnerabilities (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  project_id UUID REFERENCES public.projects(id) ON DELETE CASCADE,
  file_id UUID REFERENCES public.code_files(id) ON DELETE CASCADE,
  vulnerability_type VARCHAR(100) NOT NULL,
  severity VARCHAR(20) NOT NULL, -- 'low', 'medium', 'high', 'critical'
  description TEXT NOT NULL,
  line_number INTEGER,
  remediation TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
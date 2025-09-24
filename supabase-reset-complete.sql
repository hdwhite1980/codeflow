-- COMPLETE DATABASE RESET FOR CODEFLOW
-- WARNING: This will DELETE ALL DATA in your database!
-- Only run this if you want to start completely fresh

-- ========================================
-- STEP 1: DROP ALL EXISTING TABLES
-- ========================================

-- Drop all policies first (to avoid dependency issues)
DROP POLICY IF EXISTS "Users can view own profile" ON public.profiles;
DROP POLICY IF EXISTS "Users can update own profile" ON public.profiles;
DROP POLICY IF EXISTS "Users can insert own profile" ON public.profiles;
DROP POLICY IF EXISTS "Users can view own projects" ON public.projects;
DROP POLICY IF EXISTS "Users can insert own projects" ON public.projects;
DROP POLICY IF EXISTS "Users can update own projects" ON public.projects;
DROP POLICY IF EXISTS "Users can delete own projects" ON public.projects;
DROP POLICY IF EXISTS "Users can view code files from own projects" ON public.code_files;
DROP POLICY IF EXISTS "Users can insert code files to own projects" ON public.code_files;
DROP POLICY IF EXISTS "Users can update code files in own projects" ON public.code_files;
DROP POLICY IF EXISTS "Users can delete code files from own projects" ON public.code_files;
DROP POLICY IF EXISTS "Users can view own ai sessions" ON public.ai_sessions;
DROP POLICY IF EXISTS "Users can insert own ai sessions" ON public.ai_sessions;
DROP POLICY IF EXISTS "Users can update own ai sessions" ON public.ai_sessions;
DROP POLICY IF EXISTS "Users can view own analysis results" ON public.analysis_results;
DROP POLICY IF EXISTS "Users can insert own analysis results" ON public.analysis_results;
DROP POLICY IF EXISTS "Users can view own security vulnerabilities" ON public.security_vulnerabilities;
DROP POLICY IF EXISTS "Users can insert own security vulnerabilities" ON public.security_vulnerabilities;

-- Drop all triggers
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP TRIGGER IF EXISTS update_profiles_updated_at ON public.profiles;
DROP TRIGGER IF EXISTS update_projects_updated_at ON public.projects;
DROP TRIGGER IF EXISTS update_code_files_updated_at ON public.code_files;
DROP TRIGGER IF EXISTS update_ai_sessions_updated_at ON public.ai_sessions;

-- Drop all functions
DROP FUNCTION IF EXISTS public.handle_new_user();
DROP FUNCTION IF EXISTS public.update_updated_at_column();

-- Drop all tables (in reverse dependency order)
DROP TABLE IF EXISTS public.security_vulnerabilities CASCADE;
DROP TABLE IF EXISTS public.analysis_results CASCADE;
DROP TABLE IF EXISTS public.ai_sessions CASCADE;
DROP TABLE IF EXISTS public.code_files CASCADE;
DROP TABLE IF EXISTS public.projects CASCADE;
DROP TABLE IF EXISTS public.profiles CASCADE;

-- Drop any old tables that might exist
DROP TABLE IF EXISTS public.users CASCADE;
DROP TABLE IF EXISTS public.analysis_history CASCADE;

-- ========================================
-- STEP 2: CREATE FRESH TABLES
-- ========================================

-- Create profiles table (extends Supabase auth.users)
CREATE TABLE public.profiles (
  id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT,
  name TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (id)
);

-- Projects table
CREATE TABLE public.projects (
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

-- Code files table
CREATE TABLE public.code_files (
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

-- AI sessions table
CREATE TABLE public.ai_sessions (
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

-- Analysis results table
CREATE TABLE public.analysis_results (
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

-- Security vulnerabilities table
CREATE TABLE public.security_vulnerabilities (
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

-- ========================================
-- STEP 3: ENABLE RLS
-- ========================================

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analysis_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.security_vulnerabilities ENABLE ROW LEVEL SECURITY;

-- ========================================
-- STEP 4: CREATE RLS POLICIES
-- ========================================

-- Profiles policies
CREATE POLICY "Users can view own profile" ON public.profiles
  FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Users can update own profile" ON public.profiles
  FOR UPDATE USING (auth.uid() = id);
CREATE POLICY "Users can insert own profile" ON public.profiles
  FOR INSERT WITH CHECK (auth.uid() = id);

-- Projects policies
CREATE POLICY "Users can view own projects" ON public.projects
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own projects" ON public.projects
  FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own projects" ON public.projects
  FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "Users can delete own projects" ON public.projects
  FOR DELETE USING (auth.uid() = user_id);

-- Code files policies (through project ownership)
CREATE POLICY "Users can view code files from own projects" ON public.code_files
  FOR SELECT USING (EXISTS (
    SELECT 1 FROM public.projects WHERE projects.id = code_files.project_id AND projects.user_id = auth.uid()
  ));
CREATE POLICY "Users can insert code files to own projects" ON public.code_files
  FOR INSERT WITH CHECK (EXISTS (
    SELECT 1 FROM public.projects WHERE projects.id = code_files.project_id AND projects.user_id = auth.uid()
  ));
CREATE POLICY "Users can update code files in own projects" ON public.code_files
  FOR UPDATE USING (EXISTS (
    SELECT 1 FROM public.projects WHERE projects.id = code_files.project_id AND projects.user_id = auth.uid()
  ));
CREATE POLICY "Users can delete code files from own projects" ON public.code_files
  FOR DELETE USING (EXISTS (
    SELECT 1 FROM public.projects WHERE projects.id = code_files.project_id AND projects.user_id = auth.uid()
  ));

-- AI sessions policies
CREATE POLICY "Users can view own ai sessions" ON public.ai_sessions
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own ai sessions" ON public.ai_sessions
  FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own ai sessions" ON public.ai_sessions
  FOR UPDATE USING (auth.uid() = user_id);

-- Analysis results policies
CREATE POLICY "Users can view own analysis results" ON public.analysis_results
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own analysis results" ON public.analysis_results
  FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Security vulnerabilities policies
CREATE POLICY "Users can view own security vulnerabilities" ON public.security_vulnerabilities
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own security vulnerabilities" ON public.security_vulnerabilities
  FOR INSERT WITH CHECK (auth.uid() = user_id);

-- ========================================
-- STEP 5: CREATE INDEXES
-- ========================================

CREATE INDEX idx_profiles_email ON public.profiles(email);
CREATE INDEX idx_projects_user_id ON public.projects(user_id);
CREATE INDEX idx_code_files_project_id ON public.code_files(project_id);
CREATE INDEX idx_ai_sessions_user_id ON public.ai_sessions(user_id);
CREATE INDEX idx_ai_sessions_project_id ON public.ai_sessions(project_id);
CREATE INDEX idx_analysis_results_user_id ON public.analysis_results(user_id);
CREATE INDEX idx_analysis_results_project_id ON public.analysis_results(project_id);
CREATE INDEX idx_analysis_results_file_id ON public.analysis_results(file_id);
CREATE INDEX idx_vulnerabilities_user_id ON public.security_vulnerabilities(user_id);
CREATE INDEX idx_vulnerabilities_project_id ON public.security_vulnerabilities(project_id);
CREATE INDEX idx_vulnerabilities_file_id ON public.security_vulnerabilities(file_id);

-- ========================================
-- STEP 6: CREATE FUNCTIONS AND TRIGGERS
-- ========================================

-- Function to automatically create profile on signup
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO public.profiles (id, email, name)
  VALUES (
    NEW.id,
    NEW.email,
    COALESCE(NEW.raw_user_meta_data->>'name', split_part(NEW.email, '@', 1))
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Create trigger to automatically create profile
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for updated_at
CREATE TRIGGER update_profiles_updated_at BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_projects_updated_at BEFORE UPDATE ON public.projects
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_code_files_updated_at BEFORE UPDATE ON public.code_files
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

CREATE TRIGGER update_ai_sessions_updated_at BEFORE UPDATE ON public.ai_sessions
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at_column();

-- ========================================
-- STEP 7: GRANT PERMISSIONS
-- ========================================

GRANT USAGE ON SCHEMA public TO anon, authenticated;
GRANT ALL ON public.profiles TO authenticated;
GRANT ALL ON public.projects TO authenticated;
GRANT ALL ON public.code_files TO authenticated;
GRANT ALL ON public.ai_sessions TO authenticated;
GRANT ALL ON public.analysis_results TO authenticated;
GRANT ALL ON public.security_vulnerabilities TO authenticated;

-- ========================================
-- COMPLETION MESSAGE
-- ========================================

-- If you see this message, the reset was successful!
SELECT 'CodeFlow database reset completed successfully! All tables, policies, functions, and triggers have been created.' AS status;
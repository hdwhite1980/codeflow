-- Step 2: Enable RLS and create policies
-- Run this AFTER step 1 completes successfully

-- Enable RLS on all tables
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.code_files ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ai_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analysis_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.security_vulnerabilities ENABLE ROW LEVEL SECURITY;

-- Create policy for profiles
CREATE POLICY "Users can view own profile" ON public.profiles
  FOR SELECT USING (auth.uid() = id);
CREATE POLICY "Users can update own profile" ON public.profiles
  FOR UPDATE USING (auth.uid() = id);
CREATE POLICY "Users can insert own profile" ON public.profiles
  FOR INSERT WITH CHECK (auth.uid() = id);

-- Create RLS policies for projects
CREATE POLICY "Users can view own projects" ON public.projects
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own projects" ON public.projects
  FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own projects" ON public.projects
  FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "Users can delete own projects" ON public.projects
  FOR DELETE USING (auth.uid() = user_id);

-- Create RLS policies for code_files (through project ownership)
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

-- Create RLS policies for ai_sessions
CREATE POLICY "Users can view own ai sessions" ON public.ai_sessions
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own ai sessions" ON public.ai_sessions
  FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users can update own ai sessions" ON public.ai_sessions
  FOR UPDATE USING (auth.uid() = user_id);

-- Create RLS policies for analysis_results
CREATE POLICY "Users can view own analysis results" ON public.analysis_results
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own analysis results" ON public.analysis_results
  FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Create RLS policies for security_vulnerabilities
CREATE POLICY "Users can view own security vulnerabilities" ON public.security_vulnerabilities
  FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users can insert own security vulnerabilities" ON public.security_vulnerabilities
  FOR INSERT WITH CHECK (auth.uid() = user_id);
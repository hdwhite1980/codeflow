-- Section 3: Test code_files policies (run after projects works)
ALTER TABLE public.code_files ENABLE ROW LEVEL SECURITY;

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
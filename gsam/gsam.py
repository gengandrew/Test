import torch
from .util import enable_running_stats, disable_running_stats
import contextlib

class GSAM(torch.optim.Optimizer):
    def __init__(self, params, base_optimizer, model, gsam_alpha, rho_scheduler, adaptive=False, perturb_eps=1e-12, **kwargs):
        defaults = dict(adaptive=adaptive, **kwargs)
        super(GSAM, self).__init__(params, defaults)
        self.model = model
        self.base_optimizer = base_optimizer
        self.param_groups = self.base_optimizer.param_groups
        self.adaptive = adaptive
        self.rho_scheduler = rho_scheduler
        self.perturb_eps = perturb_eps
        self.alpha = gsam_alpha

    @torch.no_grad()
    def perturb_weights(self, rho=0.0):
        grad_norm = self._grad_norm()
        for group in self.param_groups:
            scale = rho / (grad_norm + self.perturb_eps)

            for p in group["params"]:
                if p.grad is None: continue
                self.state[p]["old_g"] = p.grad.data.clone()
                e_w = p.grad * scale.to(p)
                if self.adaptive:
                    e_w *= torch.abs(p)
                p.add_(e_w)  # climb to the local maximum "w + e(w)"
                self.state[p]['e_w'] = e_w

    @torch.no_grad()
    def gradient_decompose(self, alpha=0.0):
        # calculate inner product
        inner_prod = 0.0
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None: continue
                inner_prod += torch.sum(
                    self.state[p]['old_g'] * p.grad.data
                )

        # get norm
        new_grad_norm = self._grad_norm()
        old_grad_norm = self._grad_norm(by='old_g')

        # get cosine
        cosine = inner_prod / (new_grad_norm * old_grad_norm + self.perturb_eps)

        # gradient decomposition
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is None: continue
                vertical = self.state[p]['old_g'] - cosine * old_grad_norm * p.grad.data / (new_grad_norm + self.perturb_eps)
                p.grad.data.add_( vertical, alpha=-alpha)
                p.data.sub_(self.state[p]['e_w'])

                if torch.distributed.is_initialized(): # synchronize final gardients
                    torch.distributed.all_reduce(p.grad)

    @torch.no_grad()
    def _grad_norm(self, by=None):
        #shared_device = self.param_groups[0]["params"][0].device  # put everything on the same device, in case of model parallelism
        if not by:
            norm = torch.norm(
                    torch.stack([
                        ( p.grad).norm(p=2)
                        for group in self.param_groups for p in group["params"]
                        if p.grad is not None
                    ]),
                    p=2
               )
        else:
            norm = torch.norm(
                torch.stack([
                    (self.state[p][by]).norm(p=2)
                    for group in self.param_groups for p in group["params"]
                    if p.grad is not None
                ]),
                p=2
            )
        return norm

    def load_state_dict(self, state_dict):
        super().load_state_dict(state_dict)
        self.base_optimizer.param_groups = self.param_groups

    @torch.no_grad()
    def set_closure(self, loss_fn, inputs, targets, **kwargs):
        # create self.forward_backward_func, which is a function such that
        # self.forward_backward_func() automatically performs forward and backward passes.
        # This function does not take any arguments, and the inputs and targets data
        # should be pre-set in the definition of partial-function

        def maybe_no_sync():
            if torch.distributed.is_initialized():
                return self.model.no_sync()
            else:
                return contextlib.ExitStack()

        def get_grad():
            self.base_optimizer.zero_grad()
            with maybe_no_sync():
                with torch.enable_grad():
                    outputs = self.model(inputs)
                    loss = loss_fn(outputs, targets, **kwargs)
                loss_value = loss.data.clone().detach()
                loss.backward()
            return outputs, loss_value

        self.forward_backward_func = get_grad

    @torch.no_grad()
    def step(self, closure=None):

        if closure:
            get_grad = closure
        else:
            get_grad = self.forward_backward_func

        # get gradient
        outputs, loss_value = get_grad()

        # perturb weights
        self.perturb_weights(rho=self.rho_scheduler.step())

        # disable running stats for second pass
        disable_running_stats(self.model)

        # get gradient at perturbed weights
        get_grad()

        # decompose and get new update direction
        self.gradient_decompose(self.alpha)

        # update with new directions
        self.base_optimizer.step()

        # enable running stats
        enable_running_stats(self.model)

        return outputs, loss_value
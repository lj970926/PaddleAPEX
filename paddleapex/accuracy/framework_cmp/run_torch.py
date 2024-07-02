import argparse
import os
import time
import paddle
import torch
import copy
from tqdm import tqdm

from paddleapex.accuracy.utils import (
    print_info_log,
    check_grad_list,
    gen_api_params,
    api_json_read,
    rand_like,
    print_warn_log,
)

current_time = time.strftime("%Y%m%d%H%M%S")
Warning_list = []


dtype_mapping = {
    "FP16": paddle.float16,
    "FP32": paddle.float32,
    "BF16": paddle.bfloat16,
}

tqdm_params = {
    "smoothing": 0,  # 平滑进度条的预计剩余时间，取值范围0到1
    "desc": "Processing",  # 进度条前的描述文字
    "leave": True,  # 迭代完成后保留进度条的显示
    "ncols": 75,  # 进度条的固定宽度
    "mininterval": 0.1,  # 更新进度条的最小间隔秒数
    "maxinterval": 1.0,  # 更新进度条的最大间隔秒数
    "miniters": 1,  # 更新进度条之间的最小迭代次数
    "ascii": None,  # 根据环境自动使用ASCII或Unicode字符
    "unit": "it",  # 迭代单位
    "unit_scale": True,  # 自动根据单位缩放
    "dynamic_ncols": True,  # 动态调整进度条宽度以适应控制台
    "bar_format": "{l_bar}{bar}| {n}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",  # 自定义进度条输出
}


# Enforce the type of input tensor to be enforce_dtype
def enforce_convert(arg_in, enforce_dtype=None):
    if isinstance(arg_in, (list, tuple)):
        return type(arg_in)(enforce_convert(arg, enforce_dtype) for arg in arg_in)
    elif isinstance(arg_in, paddle.Tensor):
        if arg_in.dtype.name in dtype_mapping:
            return (
                arg_in.cast(dtype_mapping[enforce_dtype]) if enforce_dtype else arg_in
            )
        else:
            return arg_in
    else:
        return arg_in


TYPE_MAPPING = {
    "FP64": "torch.float64",
    "FP32": "torch.float32",
    "BF16": "torch.bfloat16",
    "FP16": "torch.float16",
    "BOOL": "torch.bool",
    "UINT8": "torch.uint8",
    "INT16": "torch.int16",
    "INT32": "torch.int32",
    "INT64": "torch.int64",
}


def recursive_arg_to_device(arg_in, mode="to_torch"):
    if isinstance(arg_in, (list, tuple)):
        return type(arg_in)(recursive_arg_to_device(arg, mode) for arg in arg_in)
    elif isinstance(arg_in, (paddle.Tensor, torch.Tensor)):
        type_convert = False
        if mode == "to_torch":
            grad_state = arg_in.stop_gradient
            if arg_in.dtype == paddle.bfloat16:
                type_convert = True
                arg_in = arg_in.cast(paddle.float32)
            arg_in_np = arg_in.numpy()
            arg_in = torch.from_numpy(arg_in_np)
            arg_in = arg_in.cuda()
            if type_convert:
                arg_in_device = arg_in.to(torch.bfloat16).cuda()
            else:
                arg_in_device = arg_in.cuda()
            if grad_state:
                arg_in_device.requires_grad = False
            else:
                arg_in_device.requires_grad = True
            return arg_in_device
        elif mode == "to_paddle":
            if arg_in.dtype == torch.bfloat16:
                type_convert = True
                arg_in = arg_in.to(torch.float32)
            arg_in = arg_in.cpu().detach().numpy()
            arg_in = paddle.to_tensor(arg_in)
            return arg_in.cast(paddle.bfloat16) if type_convert else arg_in
        else:
            raise ValueError(
                "recursive_arg_to_device mode must be 'to_torch' or 'to_paddle'"
            )
    elif arg_in in TYPE_MAPPING and mode == "to_torch":
        type_str = TYPE_MAPPING[arg_in.upper()]
        return eval(type_str)
    elif isinstance(arg_in, paddle.dtype) and mode == "to_torch":
        return eval(TYPE_MAPPING[arg_in.name])
    else:
        return arg_in


def ut_case_parsing(forward_content, cfg, out_path):
    print_info_log("start UT save")
    multi_dtype_ut = cfg.multi_dtype_ut.split(",") if cfg.multi_dtype_ut else []
    for item in multi_dtype_ut:
        fwd_output_dir = os.path.abspath(os.path.join(out_path, item, "output"))
        bwd_output_dir = os.path.abspath(
            os.path.join(out_path, item, "output_backward")
        )
        os.makedirs(fwd_output_dir, exist_ok=True)
        os.makedirs(bwd_output_dir, exist_ok=True)

    for i, (api_call_name, api_info_dict) in enumerate(
        tqdm(forward_content.items(), **tqdm_params)
    ):
        try:
            eval(api_call_name.split("*")[0])
        except Exception:
            paddle_api_name = api_info_dict["origin_paddle_op"]
            msg = f"{paddle_api_name} No matching api!"
            Warning_list.append(msg)
            continue
        if len(multi_dtype_ut) > 0:
            for enforce_dtype in multi_dtype_ut:
                process = (
                    api_call_name
                    + "*"
                    + enforce_dtype.__str__()
                    + "<--->"
                    + api_info_dict["origin_paddle_op"]
                )
                print(process)
                api_info_dict_copy = copy.deepcopy(api_info_dict)
                fwd_res, bp_res = run_api_case(
                    api_call_name, api_info_dict_copy, enforce_dtype
                )
                paddle_name = api_info_dict["origin_paddle_op"]
                fwd_output_path = os.path.join(
                    out_path, enforce_dtype, "output", paddle_name
                )
                bwd_output_path = os.path.join(
                    out_path, enforce_dtype, "output_backward", paddle_name
                )
                fwd_res = recursive_arg_to_device(fwd_res, mode="to_paddle")
                bp_res = recursive_arg_to_device(bp_res, mode="to_paddle")
                if not isinstance(fwd_res, type(None)):
                    fwd_res = recursive_arg_to_device(fwd_res, mode="to_paddle")
                    paddle.save(fwd_res, fwd_output_path)
                if not isinstance(bp_res, type(None)):
                    bp_res = recursive_arg_to_device(bp_res, mode="to_paddle")
                    paddle.save(bp_res, bwd_output_path)
                print("*" * 100)
        else:
            print(api_call_name)
            fwd_res, bp_res = run_api_case(api_call_name, api_info_dict)
            paddle_name = api_info_dict["origin_paddle_op"]
            fwd_output_path = os.path.join(
                out_path, enforce_dtype, "output", paddle_name
            )
            bwd_output_path = os.path.join(
                out_path, enforce_dtype, "output_backward", paddle_name
            )
            if not isinstance(fwd_res, type(None)):
                fwd_res = recursive_arg_to_device(fwd_res, mode="to_paddle")
                paddle.save(fwd_res, fwd_output_path)
            if not isinstance(bp_res, type(None)):
                bp_res = recursive_arg_to_device(bp_res, mode="to_paddle")
                paddle.save(bp_res, bwd_output_path)
            print("*" * 100)


def run_api_case(api_call_name, api_info_dict, enforce_dtype=None):
    api_call_stack = api_call_name.rsplit("*")[0]

    # generate paddle tensor
    args, kwargs, need_backward = gen_api_params(api_info_dict)
    ##################################################################
    ##      RUN FORWARD
    ##################################################################
    try:
        # dtype convert
        if enforce_dtype:
            args = enforce_convert(args, enforce_dtype)
            kwargs = {
                key: enforce_convert(value, enforce_dtype)
                for key, value in kwargs.items()
            }
        # paddle to torch
        device_args = recursive_arg_to_device(args, mode="to_torch")
        device_kwargs = {
            key: recursive_arg_to_device(value, mode="to_torch")
            for key, value in kwargs.items()
        }
        device_out = eval(api_call_stack)(*device_args, **device_kwargs)

    except Exception as err:
        api_name = api_call_name.split("*")[0]
        msg = f"Run API {api_name} Forward Error: %s" % str(err)
        print_warn_log(msg)
        Warning_list.append(msg)
        return None, None

    ##################################################################
    ##      RUN BACKWARD
    ##################################################################
    if need_backward:
        try:
            device_grad_out = []
            out = recursive_arg_to_device(device_out, mode="to_paddle")
            dout = rand_like(out)
            dout = recursive_arg_to_device(dout, mode="to_torch")
            torch.autograd.backward([device_out], [dout])
            for arg in device_args:
                if isinstance(arg, torch.Tensor):
                    device_grad_out.append(arg.grad)
                if isinstance(arg, list):  # op: concat/stack
                    for x in arg:
                        if isinstance(x, torch.Tensor):
                            device_grad_out.append(x.grad)
            for k, v in device_kwargs.items():
                if isinstance(v, torch.Tensor):
                    device_grad_out.append(v.grad)
                if isinstance(v, list):  # op: concat
                    for x in v:
                        if isinstance(x, torch.Tensor):
                            device_grad_out.append(x.grad)
            device_grad_out = check_grad_list(device_grad_out)
            if device_grad_out is None:
                msg = f"{api_call_name} grad_list is None"
                Warning_list.append(msg)
        except Exception as err:
            api_name = api_call_name.split("*")[0]
            msg = f"Run API {api_name} backward Error: %s" % str(err)
            print_warn_log(msg)
            Warning_list.append(msg)
            return device_out, None
    else:
        msg = f"{api_call_name} has no tensor required grad, SKIP Backward"
        print_warn_log(msg)
        Warning_list.append(msg)
        return device_out, None
    return device_out, device_grad_out


def arg_parser(parser):
    parser.add_argument(
        "-json",
        "--json",
        dest="json_path",
        default="",
        type=str,
        help="Dump json file path",
        required=True,
    )
    parser.add_argument(
        "-out",
        "--dump_path",
        dest="out_path",
        default="./paddle",
        type=str,
        help="<optional> The ut task result out path.",
        required=False,
    )
    parser.add_argument(
        "-enforce-dtype",
        "--dtype",
        dest="multi_dtype_ut",
        default="FP32,FP16,BF16",
        type=str,
        help="",
        required=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    arg_parser(parser)
    cfg = parser.parse_args()
    forward_content = api_json_read(cfg.json_path)
    out_path = os.path.realpath(cfg.out_path) if cfg.out_path else "./"
    ut_case_parsing(forward_content, cfg, out_path)
    warning_log_pth = os.path.join(out_path, "./warning_log.txt")
    File = open(warning_log_pth, "w")
    for item in Warning_list:
        File.write(item + "\n")
    File.close()
    print_info_log("UT save completed")